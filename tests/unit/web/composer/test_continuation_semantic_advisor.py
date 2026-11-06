"""Current semantic phase contracts from a real paid PRIMARY and refused tool.

Provider calls use original V4 preparation, admission and completion owners.
Altered-frontier tests exercise request/guard DATA only, never SQL admission.
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
from pydantic import ValidationError

from elspeth.contracts.composer_content import ComposerContentScope
from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.prompt_guard import PROMPT_GUARD_MAX_TEXT_CHARACTERS
from elspeth.web.composer import prompt_guard, provider_gateway
from elspeth.web.composer.continuation import ComposeFrame, ToolBatchFrame
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import (
    decode_continuation_response,
    decode_retained_physical_audit_call,
    encode_continuation_snapshot,
    encode_frontier_chunk,
    encode_private_chunk,
)
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.continuation_semantic_advisor import SemanticAdvisorReviewInput, prepare_semantic_advisor_request
from elspeth.web.composer.continuation_semantic_invocation import make_semantic_advisor_invocation
from elspeth.web.composer.continuation_semantic_source import capture_semantic_advisor_source
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.prompt_guard import PromptGuardMount, PromptGuardRefused
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.composer.provider_context import capture_provider_context
from elspeth.web.composer.provider_cost import CommonProviderCostRefused
from elspeth.web.composer.provider_gateway import _MalformedLLMResponseError
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_semantic_advisor import read_semantic_advisor_retained_inputs_on_connection
from elspeth.web.sessions.continuation_semantic_review_result import (
    RetainedSemanticReviewResult,
    decode_semantic_review_result,
    encode_semantic_review_provider_input,
)
from tests.unit.web.composer.test_advisor_invocation_outcome import operation_store as _operation_store
from tests.unit.web.composer.test_prompt_guard_admission import _checks
from tests.unit.web.sessions.test_continuation_tool_failure import _advisor_case
from tests.unit.web.sessions.test_token_usage_adapters import _ledger

operation_store = _operation_store


@asynccontextmanager
async def _semantic_entry(store, tmp_path, monkeypatch):
    """Run the real post-batch driver, hand over before semantic, then reclaim."""
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
    from elspeth.web.composer.provider_quota import ProviderInvocationOwner
    from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption
    from elspeth.web.sessions.composer_operations import ComposerOperationRunning
    from elspeth.web.sessions.continuation_driver import ContinuationSegmentHandedOver, _SegmentDriver
    from tests.unit.web.sessions import test_continuation_composition_initialization as initialization
    from tests.unit.web.sessions.test_continuation_tool_failure import _claimed_driver, _install_original_deadline, _unstarted_driver

    _install_original_deadline(store, monkeypatch)
    async with _advisor_case(
        store,
        tmp_path,
        monkeypatch,
        tool_name="get_plugin_schema",
        tool_arguments={"plugin_type": "source", "name": "K7_REFUSED_PLUGIN"},
    ) as (owner, running, paid):
        timing = owner.work.coordinator.sql_deadline_owner
        assert timing is not None
        driver = _unstarted_driver(store, owner, running, drain_signal=timing.drain_signal)
        actual_advance = _SegmentDriver.advance
        ready_cuts = []

        async def no_prelude_sdk(**_request):
            raise AssertionError("refused discovery prelude must stop before its first semantic SDK call")

        async def observe_advance(current, stored, selected):
            result = await actual_advance(current, stored, selected)
            if current is driver and result is None:
                following, after = await current.selected()
                stage = await current.iteration_stage(following, after)
                top = after.stack.frames[-1]
                returned = type(top) is ToolBatchFrame and stage is not None and stage.stage == "returned"
                advanced = type(top) is ComposeFrame and top.phase == "action_ready" and after.stack.active_action_json is None
                if returned or advanced:
                    assert not ready_cuts
                    if type(top) is ComposeFrame:
                        assert top.iteration is not None
                    ready_cuts.append(after)
                    timing.drain_signal.request()
            return result

        with monkeypatch.context() as capture:
            capture.setattr(_SegmentDriver, "advance", observe_advance)
            capture.setattr(litellm, "acompletion", no_prelude_sdk)
            async with _claimed_driver(driver):
                handover = await driver.run(None)
        assert type(handover) is ContinuationSegmentHandedOver and len(ready_cuts) == 1
        assert timing.drain_signal.draining.is_set() and timing.drain_signal.started_at is not None
        drain_deadline = driver.worker.drain_deadline
        assert drain_deadline is not None
        assert drain_deadline.expires_at == timing.drain_signal.started_at + driver.worker._drain_seconds
        store[1].verify_recovery(handover.receipt)
        service = owner.service
    owner.work.coordinator.assert_completed()

    (claim,) = store[2].claim_segment(limit=1)
    assert claim.operation_id == running.claim.operation_id
    assert claim.ownership_generation == running.claim.ownership_generation + 1
    context = store[1].start_composer_segment(
        claim, owner_instance_id=store[2]._owner_instance_id, lease_seconds=30, auth_provider_type="local"
    )
    current = ComposerOperationRunning(claim, context)
    work = initialization.inventory_work(current)
    lease = await ContinuationLeaseAdoption(store[1], context, lease_seconds=30, required_work=work.coordinator).join()
    try:
        provider = ProviderInvocationOwner(service=store[3], required_work=work)
        phases = ContinuationLivePhases(service, store[2], work, provider, lease)
        head = store[2].load_continuation_snapshot(current)
        selected = await load_selected_continuation(authority=store[2], running=current, selected=head, required_work=work)
        assert selected.stack == ready_cuts[0].stack.model_copy(update={"cursor": head.cursor})
        await phases.current(selected)
        yield phases, current, paid
    finally:
        await lease.close()
        work.coordinator.assert_completed()


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
    async with _semantic_entry(store, tmp_path, monkeypatch) as (phases, running, paid):
        preparing_fixture = False
        runtime = phases.runtime
        assert runtime is not None
        state = runtime.live.state
        provider = runtime.bindings.provider_owner
        advisor = phases.service._advisor_checkpoint
        context = running.session_operation_context
        coordinator = phases.work.coordinator
        user = runtime.bindings.message
        invocation = make_semantic_advisor_invocation(capture_semantic_advisor_source(runtime))
        frontier, source = invocation.frontier, invocation.invocation
        assert source.iteration is not None
        selected = runtime.source.selected
        stored = StoredContinuationSnapshot(encode_continuation_snapshot(selected.stack), selected.stack.cursor, selected.checkpoint_id)
        with store[0].connect() as conn:
            retained_inputs = read_semantic_advisor_retained_inputs_on_connection(conn, running, selected=stored)
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
            "retained_inputs": retained_inputs,
        }
        baseline = {"ledger": len(_ledger(store[0])), "checks": len(_checks(store[0])), "paid": len(paid)}
        baseline["phases"] = phases
        baseline["recorder"] = len(runtime.live.recorder.llm_calls)
        assert baseline["paid"] == 1
        (refused,) = [
            item
            for item in runtime.live.recorder.invocations
            if item.tool_name == "get_plugin_schema" and json.loads(item.arguments_canonical)["name"] == "K7_REFUSED_PLUGIN"
        ]
        assert refused.error_class is not None or (
            refused.result_canonical is not None and json.loads(refused.result_canonical)["success"] is False
        )
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


async def _observe_case(phases, monkeypatch):
    """Observe one actual V4 phase, preserving its returned terminal or error."""
    from elspeth.web.composer import continuation_semantic_advisor_phase as phase

    runtime = phases.runtime
    assert runtime is not None
    clock = ContinuationActionClock(runtime.source.policy.actions.advisor)
    phases.bind_action_clock(clock)
    deadline = phases.work.coordinator.sql_deadline_owner
    if deadline is not None:
        deadline.begin_action(clock.policy, started_at=clock.started_monotonic)
    actual = phase.observe_semantic_advisor_invocation
    observations = []

    async def observe(request, **kwargs):
        outcome = await actual(request, **kwargs)
        observations.append((outcome, request))
        return outcome

    terminal = error = None
    with monkeypatch.context() as capture:
        capture.setattr(phase, "observe_semantic_advisor_invocation", observe)
        try:
            terminal = await phase.run_continuation_semantic_advisor(phases, runtime.source.selected)
        except BaseException as original:
            error = original
    if not observations:
        assert error is not None
        raise error
    assert len(observations) == 1
    outcome, request = observations[0]
    return outcome, terminal, error, request


async def _run_case(phases, monkeypatch):
    from elspeth.web.composer.continuation_semantic_advisor_outcome import SemanticAdvisorInvocationV4Finalized

    outcome, terminal, error, _request = await _observe_case(phases, monkeypatch)
    if error is not None:
        raise error
    assert terminal is None and type(outcome) is SemanticAdvisorInvocationV4Finalized
    return outcome.finalization.result


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


def _inputs_with_frontier_description(inputs, description: str):
    frontier = inputs["frontier"]
    obligation = replace(frontier.obligations[0], description=description)
    changed = replace(frontier, obligations=(obligation, *frontier.obligations[1:]))
    source = inputs["source"]
    premise = source.premise
    assert premise is not None
    rebound = source.model_copy(update={"premise": premise.model_copy(update={"frontier": encode_frontier_chunk(changed).reference})})
    return {**inputs, "source": rebound, "frontier": changed}


def _guard_data(request):
    """Exercise guard rendering DATA without claiming current SQL admission."""
    prepared = request.prepared
    assert prepared.mount is not None
    evidence = prepared.evidence
    return prompt_guard._request(
        policy_fingerprint=prepared.mount.client.policy_fingerprint,
        role="advisor",
        messages=evidence.messages,
        tools=(),
        user_prompt=prepared.genuine_user,
        trusted_message_indexes=evidence.trusted_message_indexes,
        content_parts=evidence.content_parts,
        tool_argument_parts=evidence.tool_argument_parts,
        thinking_parts=evidence.thinking_parts,
    )


@pytest.mark.asyncio
async def test_semantic_frontier_wrapper_partitions_without_changing_prepared_wire_or_hash(operation_store, tmp_path, monkeypatch):
    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, _provider, _context, coordinator, baseline, _custodies):
        original_frontier = inputs["frontier"]
        original_text = original_frontier.obligations[0].description
        wrapper_overhead = len(encode_frontier_chunk(original_frontier).canonical) - len(original_text)
        description = "u" * (PROMPT_GUARD_MAX_TEXT_CHARACTERS + 1 - wrapper_overhead)
        assert 0 < len(description) < PROMPT_GUARD_MAX_TEXT_CHARACTERS - 100
        changed_inputs = _inputs_with_frontier_description(inputs, description)
        request = prepare_semantic_advisor_request(**changed_inputs)
        original_request_sha256 = request.request_sha256
        retained = request.prepared.evidence.messages[-1]["content"]
        parts = request.prepared.evidence.content_parts[-1]
        assert type(retained) is str and parts is not None
        assert len(encode_frontier_chunk(changed_inputs["frontier"]).canonical) == PROMPT_GUARD_MAX_TEXT_CHARACTERS + 1
        assert json.loads(retained)["frontier"] == encode_frontier_chunk(changed_inputs["frontier"]).canonical
        assert retained == canonical_json(json.loads(retained))
        assert "".join(part.text for part in parts) == retained
        assert sum(part.inspection_text == description for part in parts) == 1
        request.verify()
        guarded, supported = _guard_data(request)
        assert supported and guarded.role == "advisor"
        assert request.request_sha256 == original_request_sha256
        assert len(_checks(operation_store[0])) == baseline["checks"]
        assert len(_ledger(operation_store[0])) == baseline["ledger"]
        coordinator.assert_completed()


@pytest.mark.asyncio
async def test_semantic_frontier_indivisible_scalar_above_limit_is_unsupported_guard_data(operation_store, tmp_path, monkeypatch):
    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, _provider, _context, coordinator, baseline, custodies):
        description = "u" * (PROMPT_GUARD_MAX_TEXT_CHARACTERS + 1)
        request = prepare_semantic_advisor_request(**_inputs_with_frontier_description(inputs, description))
        parts = request.prepared.evidence.content_parts[-1]
        assert parts is not None and sum(part.inspection_text == description for part in parts) == 1
        _guarded, supported = _guard_data(request)
        assert supported is False and custodies == []
        assert len(_ledger(operation_store[0])) == baseline["ledger"]
        assert len(_checks(operation_store[0])) == baseline["checks"]
        coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized_public_message", [False, True], ids=["private-context-excluded", "public-message-still-bounded"])
async def test_semantic_review_provider_view_excludes_private_context_without_relaxing_guard_data(
    operation_store, tmp_path, monkeypatch, oversized_public_message
):
    """Build actual Review DATA without claiming that a guard grants SQL authority."""
    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, _provider, _context, coordinator, baseline, custodies):
        private_text = "PRIVATE_REVIEW_CONTEXT " + "x" * (PROMPT_GUARD_MAX_TEXT_CHARACTERS + 1)
        private_context = capture_provider_context(messages=[{"role": "user", "content": private_text}], tools=None)
        assert private_context.disposition == "retained" and private_context.canonical is not None
        assert len(private_context.canonical) > PROMPT_GUARD_MAX_TEXT_CHARACTERS
        recorder = inputs["runtime"].live.recorder
        assert recorder.llm_calls and recorder.invocations
        original_call = recorder.llm_calls[-1]
        call = replace(original_call, private_request=private_context)
        assert call.to_dict() == original_call.to_dict()
        message = "Review needs another authoring step."
        if oversized_public_message:
            message = "r" * (PROMPT_GUARD_MAX_TEXT_CHARACTERS + 1)
        retained_review = RetainedSemanticReviewResult(
            ComposerResult(
                message=message,
                state=inputs["state"],
                llm_calls=(call,),
                tool_invocations=tuple(recorder.invocations),
            ),
            publication_binding=None,
        )
        encoded, provider_json = encode_semantic_review_provider_input(retained_review)
        assert decode_semantic_review_result(encoded) == retained_review
        assert private_text in encoded
        retained_inputs = inputs["retained_inputs"]
        assert retained_inputs is not None
        changed_inputs = {
            **inputs,
            "retained_inputs": replace(
                retained_inputs,
                review=SemanticAdvisorReviewInput(
                    result_json=encoded,
                    owner_effect_key="end:review:guard-data-control",
                    completed_checkpoint_id=retained_inputs.checkpoint_id,
                    completed_cursor=retained_inputs.cursor,
                    outcome="blocked",
                    provider_json=provider_json,
                ),
                iteration_return=None,
            ),
        }
        # Only the request builder and guard rendering consume this input. It
        # deliberately has no SQL Review commit or authority-producing call.
        from elspeth.web.sessions import continuation_semantic_review_result as review_codec

        original_decoder = review_codec.decode_semantic_review_result
        repeated_decodes = []

        def observe_decode(value):
            repeated_decodes.append(value)
            return original_decoder(value)

        monkeypatch.setattr(review_codec, "decode_semantic_review_result", observe_decode)
        assert review_codec.decode_semantic_review_result(encoded) == retained_review
        assert repeated_decodes == [encoded]
        repeated_decodes.clear()
        request = prepare_semantic_advisor_request(**changed_inputs)
        assert repeated_decodes == []
        request.verify()
        document_text = request.prepared.evidence.messages[-1]["content"]
        assert type(document_text) is str
        document = json.loads(document_text)
        expected = json.loads(encoded)
        expected["llm_calls"] = [call.to_dict()]
        assert document["review_result"] == expected
        assert document["review_result"]["tool_invocations"] == json.loads(encoded)["tool_invocations"]
        (public_call,) = document["review_result"]["llm_calls"]
        assert public_call["call_id"] == original_call.call_id
        assert public_call["status"] == original_call.status.value
        assert {"private_request", "private_diagnostics", "private_failure"}.isdisjoint(public_call)
        assert "PRIVATE_REVIEW_CONTEXT" not in document_text
        assert document["review_result"]["message"] == message
        guarded, supported = _guard_data(request)
        assert guarded.role == "advisor" and supported is (not oversized_public_message)
        if oversized_public_message:
            parts = request.prepared.evidence.content_parts[-1]
            assert parts is not None and sum(part.inspection_text == message for part in parts) == 1
        assert custodies == []
        assert len(_checks(operation_store[0])) == baseline["checks"]
        assert len(_ledger(operation_store[0])) == baseline["ledger"]
        coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("router", [False, True])
async def test_semantic_call_uses_real_guard_quota_audit_and_exact_schema(operation_store, tmp_path, monkeypatch, router):
    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, _provider, context, coordinator, baseline, custodies):
        if router:
            monkeypatch.setattr(inputs["owner"], "_advisor_endpoint_base_url", "https://openrouter.ai/api/v1")
        original = prepare_semantic_advisor_request(**inputs)
        request = replace(original)
        assert request.source is original.source and request.prepared is original.prepared
        assert request.request_sha256 == original.request_sha256
        assert deep_thaw(request.options) == deep_thaw(original.options)
        request.verify()
        recorder = inputs["runtime"].live.recorder
        physical = []

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
        result = await _run_case(baseline["phases"], monkeypatch)
        assert len(physical) == 1 and len(recorder.llm_calls) == baseline["recorder"] + 1
        assert len(_ledger(operation_store[0])) == baseline["ledger"] + 1
        assert result.audit_call == recorder.llm_calls[-1]
        assert result.decision.decision_id == result.audit_call.call_id == physical[0].attempt_id
        assert result.audit_call.status is ComposerLLMCallStatus.SUCCESS
        assert custodies[0].attempt is custodies[0].call is None
        # The module returned historical DATA, without executing the advice.
        assert request.frontier.action_receipts == ()
        coordinator.assert_completed()


@pytest.mark.asyncio
async def test_semantic_result_retains_exact_original_response_and_physical_audit(operation_store, tmp_path, monkeypatch):
    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, _provider, _context, coordinator, baseline, _custodies):
        request = prepare_semantic_advisor_request(**inputs)
        original = "\n" + json.dumps(_answer(request), indent=2, ensure_ascii=False) + "\n"

        async def completion(**kwargs):
            return _reply(original)

        monkeypatch.setattr(litellm, "acompletion", completion)
        recorder = inputs["runtime"].live.recorder
        result = await _run_case(baseline["phases"], monkeypatch)
        assert result.response.reference.codec == "composer.response.v1"
        retained = decode_continuation_response(result.response)
        assert retained.message.content == original
        assert json.loads(original).get("decision_id") is None
        assert decode_retained_physical_audit_call(result.response) == result.audit_call
        assert result.audit_call == recorder.llm_calls[-1]
        assert len(_ledger(operation_store[0])) == baseline["ledger"] + 1
        coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", ["not JSON", '{"schema_version":"composer.continuation-decision.v1","decision":"CONTINUE","decision":"STOP_CONFIRMED"}']
)
async def test_invalid_semantic_decision_is_malformed_actual_audit(operation_store, tmp_path, monkeypatch, body):
    from elspeth.web.composer.continuation_semantic_advisor_outcome import SemanticAdvisorInvocationV4Finalized
    from elspeth.web.composer.continuation_semantic_advisor_phase import SemanticAdvisorPaidInvalidTerminal

    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, _provider, _context, coordinator, baseline, custodies):
        recorder = inputs["runtime"].live.recorder

        async def completion(**kwargs):
            return _reply(body)

        monkeypatch.setattr(litellm, "acompletion", completion)
        outcome, terminal, error, _request = await _observe_case(baseline["phases"], monkeypatch)
        assert type(outcome) is SemanticAdvisorInvocationV4Finalized and error is None
        assert type(terminal) is SemanticAdvisorPaidInvalidTerminal
        paid = outcome.finalization.result
        assert terminal.paid is paid and paid.outcome == "paid_invalid"
        assert isinstance(paid.original_error, _MalformedLLMResponseError)
        assert terminal.original_error is paid.original_error is outcome.observation.original_failure
        assert paid.decision is None and decode_continuation_response(paid.response).message.content == body
        assert len(recorder.llm_calls) == baseline["recorder"] + 1
        assert len(_ledger(operation_store[0])) == baseline["ledger"] + 1
        assert recorder.llm_calls[-1].status is ComposerLLMCallStatus.MALFORMED_RESPONSE
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
        recorder, physical = inputs["runtime"].live.recorder, []

        async def completion(**kwargs):
            physical.append(kwargs)
            assert kwargs["max_tokens"] == 23
            return _reply(json.dumps(_answer(request)))

        monkeypatch.setattr(litellm, "acompletion", completion)
        result = await _run_case(baseline["phases"], monkeypatch)
        assert result.audit_call.max_completion_tokens_requested == 23
        assert result.audit_call.provider_cost == 0.01
        assert budget.snapshot().physical_calls == before.physical_calls + 1
        assert len(budget.snapshot().charges) == len(before.charges) + 1
        assert budget.snapshot().charges[-1].response_accountable is True
        with pytest.raises(CommonProviderCostRefused, match="PROVIDER_CALLS_EXHAUSTED"):
            budget.require_dispatch()
        assert len(physical) == 1 and len(recorder.llm_calls) == baseline["recorder"] + 1
        assert len(_ledger(operation_store[0])) == baseline["ledger"] + 1
        coordinator.assert_completed()


@pytest.mark.asyncio
async def test_semantic_cancellation_keeps_original_and_one_cancelled_audit(operation_store, tmp_path, monkeypatch):
    from elspeth.web.sessions.continuation_sdk_terminal_result import SdkTerminalFailure, SdkTerminalResult
    from tests.unit.web.composer.test_continuation_semantic_advisor_outcome import _contains_original

    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, _provider, _context, coordinator, baseline, custodies):
        recorder, entered, pending, originals = inputs["runtime"].live.recorder, asyncio.Event(), asyncio.Event(), []

        async def completion(**kwargs):
            entered.set()
            try:
                await pending.wait()
            except asyncio.CancelledError as original:
                originals.append(original)
                raise

        monkeypatch.setattr(litellm, "acompletion", completion)
        task = asyncio.create_task(_observe_case(baseline["phases"], monkeypatch))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel("original semantic cancellation")
        outcome, terminal, error, _request = await task
        # wait_for cancels its separate SDK child; retain the owner's actual
        # original cancellation, rather than requiring both Tasks' objects to
        # have the same identity.
        assert len(originals) == 1 and task.cancelling() == 1
        assert isinstance(outcome.observation.original_failure, asyncio.CancelledError)
        assert outcome.observation.original_failure.args == ("original semantic cancellation",)
        assert error is None and type(terminal) in (SdkTerminalResult, SdkTerminalFailure)
        if type(terminal) is SdkTerminalFailure:
            assert terminal.original is outcome.error and _contains_original(outcome.error, outcome.observation.original_failure)
        else:
            assert outcome.error.__cause__ is outcome.observation.original_failure
        assert terminal.verified.hold_state == "consumed"
        assert terminal.verified.provider_attempt_id == outcome.observation.attempt.attempt_id
        assert len(recorder.llm_calls) == baseline["recorder"] + 1 and len(_ledger(operation_store[0])) == baseline["ledger"] + 1
        assert recorder.llm_calls[-1].status is ComposerLLMCallStatus.CANCELLED
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
        invalid_options = deep_thaw(request.options)
        invalid_options["max_tokens"] = str(invalid_options["max_tokens"])
        with pytest.raises(ValidationError):
            replace(request, options=invalid_options)
        invalid_options = deep_thaw(request.options)
        invalid_options["unexpected_option"] = True
        with pytest.raises(ValidationError):
            replace(request, options=invalid_options)
        invalid_options = deep_thaw(request.options)
        invalid_options["response_format"]["json_schema"]["schema"]["title"] = "Changed semantic response schema"
        with pytest.raises(AuditIntegrityError, match="owned decision schema"):
            replace(request, options=invalid_options)
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
            with pytest.raises(AuditIntegrityError, match="another exact service"):
                inputs["owner"]._new_advisor_custody(foreign)
            assert calls == [] and custodies == []
            assert len(_ledger(operation_store[0])) == baseline["ledger"]
            assert len(_checks(operation_store[0])) == baseline["checks"]
            coordinator.assert_completed()
        finally:
            foreign_engine.dispose()


@pytest.mark.asyncio
async def test_semantic_guard_refusal_keeps_original_owned_audit_without_sdk_or_attachment(operation_store, tmp_path, monkeypatch):
    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, _provider, _context, coordinator, baseline, custodies):
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
        recorder = inputs["runtime"].live.recorder
        with pytest.raises(PromptGuardRefused) as caught:
            await _run_case(baseline["phases"], monkeypatch)
        assert len(originals) == 1 and caught.value is originals[0]
        assert guard_requests and caught.value.disposition == "blocked"
        assert caught.value.llm_calls == () and len(recorder.llm_calls) == baseline["recorder"]
        assert sdk == [] and custodies[0].attempt is None
        checks = _checks(operation_store[0])
        assert len(checks) == baseline["checks"] + 1
        assert checks[-1]["audit_id"] == caught.value.audit_id and checks[-1]["disposition"] == "blocked"
        assert len(_ledger(operation_store[0])) == baseline["ledger"]
        coordinator.assert_completed()
