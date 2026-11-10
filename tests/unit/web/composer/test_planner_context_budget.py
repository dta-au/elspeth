"""The planner fits every provider request to the operator's context budget.

Each test drives the real planner against the scripted ``litellm.acompletion``
of ``test_pipeline_planner``, so what is asserted is the request that would have
been sent and the call record that describes it. Thresholds are derived from a
measuring run under a cap far above the request (the negative control), so the
tests do not pin the byte size of the skill, the reference block or the tools.
"""

from __future__ import annotations

import json
import math
import time
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from elspeth.contracts.composer_llm_audit import (
    ComposerContextBudgetRecord,
    ComposerLLMCall,
    ComposerLLMCallStatus,
    ComposerLLMRequestByteSplit,
)
from elspeth.contracts.composer_planner_audit import ComposerPlannerAttemptOutcome, ComposerPlannerAttemptPhase, ComposerPlannerCode
from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.prompt_guard import PROMPT_GUARD_CONTENT_PARTS_KEY, detach_prompt_guard_evidence
from elspeth.core.canonical import canonical_json
from elspeth.web.composer.advisor_transcript import CONTEXT_BUDGET_TRANSCRIPT_OMISSION_GAP, AdvisorVisibleTranscript
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.context_budget import CONTEXT_BUDGET_TOOL_RESULT_STUB, ContextBudgetPolicy
from elspeth.web.composer.context_budget_apply import (
    CONTEXT_BUDGET_OMISSION_ALLOWANCE_BYTES,
    CONTEXT_BUDGET_TRANSCRIPT_GAP_ALLOWANCE_BYTES,
    largest_component,
)
from elspeth.web.composer.context_budget_history import ContextBudgetHistory
from elspeth.web.composer.llm_response_parsing import _hash_and_measure_request, build_llm_call_record
from elspeth.web.composer.pipeline_planner import (
    _HATCH_TRANSCRIPT_HEADER,
    PipelinePlannerError,
    PlannerConversationContext,
    PlannerDeclined,
    PlannerPriorAssistantMessage,
    PlannerPriorUserRequest,
    _fit_planner_context_budget,
    _hatch_transcript_message,
    _planner_context_budget_components,
    _planner_request_message,
    _planner_request_size,
    _PlannerAttemptTrail,
)
from elspeth.web.composer.tools import ToolContext
from tests.unit.web.composer.test_pipeline_planner import (
    _budget,
    _context_policy,
    _empty_state,
    _pipeline,
    _plan,
    _response_with_call_id,
    _ScriptedCompletion,
    _text_response,
)

_ANTHROPIC = "anthropic/claude-planner"
_UNMARKED = "openai/gpt-planner"
_FALLBACK_RATIO = 3.0
_ALLOWANCE = CONTEXT_BUDGET_OMISSION_ALLOWANCE_BYTES
_TRANSCRIPT_ALLOWANCE = CONTEXT_BUDGET_TRANSCRIPT_GAP_ALLOWANCE_BYTES


def _entry_saving(index: int, content: str) -> int:
    """Bytes an entry occupies inside the request message, plus its separator."""
    return len(canonical_json({"history_index": index, "content": content}).encode("utf-8")) + 1


def _sent_bytes(request: dict[str, Any]) -> int:
    """A sent request on the measure the decision and the final request byte check use."""
    return _planner_request_size(request["messages"], request["tools"])


def _payload(request: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(request["messages"][2]["content"])
    assert type(payload) is dict
    return payload


def _sent_matches_record(request: dict[str, Any], call: ComposerLLMCall) -> None:
    messages_hash, tools_hash, split = _hash_and_measure_request(request["messages"], request["tools"])
    assert call.messages_hash == messages_hash
    assert call.tools_spec_hash == tools_hash
    assert call.request_byte_split == split


async def _decline(
    tmp_path: Path,
    tool_context: ToolContext,
    context: PlannerConversationContext,
    *,
    policy: ContextBudgetPolicy | None = None,
    history: ContextBudgetHistory | None = None,
    model: str = _UNMARKED,
) -> tuple[dict[str, Any], ComposerLLMCall]:
    """Run one planner call that ends with a text reply; return the sent request and its record."""
    completion = _ScriptedCompletion(_text_response("Which output format do you want?"))
    recorder = BufferingRecorder()
    with pytest.raises(PlannerDeclined):
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            recorder=recorder,
            conversation_context=context,
            context_policy=policy,
            context_budget_history=history,
            model_overrides={"model_identifier": model},
        )
    assert len(completion.requests) == 1 and len(recorder.llm_calls) == 1
    _sent_matches_record(completion.requests[0], recorder.llm_calls[0])
    return completion.requests[0], recorder.llm_calls[0]


def _policy_shedding_to(low_bytes: int, *, shed_above: float = 0.85, shed_to: float = 0.6) -> ContextBudgetPolicy:
    """A policy whose low-water mark, at the fallback ratio, admits at least ``low_bytes``."""
    return _context_policy(
        max_prompt_tokens=math.ceil(low_bytes / _FALLBACK_RATIO / shed_to),
        max_request_bytes=1_000_000,
        shed_above=shed_above,
        shed_to=shed_to,
    )


# --- PlannerConversationContext ---------------------------------------------------------------


def _context(*, users: dict[int, str], assistants: dict[int, str] | None = None) -> PlannerConversationContext:
    return PlannerConversationContext(
        prior_user_requests=tuple(PlannerPriorUserRequest(history_index=index, content=text) for index, text in users.items()),
        prior_assistant_messages=tuple(
            PlannerPriorAssistantMessage(history_index=index, content=text) for index, text in (assistants or {}).items()
        ),
    )


def test_without_removes_entries_and_counts_them_by_kind() -> None:
    context = _context(users={0: "u0", 2: "u2", 4: "u4"}, assistants={1: "a1", 3: "a3"})

    reduced = context.without(frozenset({0, 1, 3}))

    assert reduced.to_dict() == {
        "prior_user_requests": [{"history_index": 2, "content": "u2"}, {"history_index": 4, "content": "u4"}],
        "additional_prior_user_requests_omitted": 1,
        "prior_assistant_messages_omitted": 2,
    }
    # Counts accumulate across reductions.
    assert reduced.without(frozenset({2})).additional_prior_user_requests_omitted == 2


def test_context_may_retain_nothing_only_when_something_was_omitted() -> None:
    everything = _context(users={0: "u0"}, assistants={1: "a1"}).without(frozenset({0, 1}))
    assert everything.to_dict() == {
        "prior_user_requests": [],
        "additional_prior_user_requests_omitted": 1,
        "prior_assistant_messages_omitted": 1,
    }
    assert PlannerConversationContext(prior_user_requests=(), prior_assistant_messages_omitted=1).to_dict() == {
        "prior_user_requests": [],
        "additional_prior_user_requests_omitted": 0,
        "prior_assistant_messages_omitted": 1,
    }
    with pytest.raises(ValueError, match="must not be empty"):
        PlannerConversationContext(prior_user_requests=())


def test_omitted_requests_no_longer_require_two_retained_requests() -> None:
    one_retained = PlannerConversationContext(
        prior_user_requests=(PlannerPriorUserRequest(history_index=5, content="u5"),), additional_prior_user_requests_omitted=3
    )
    assert one_retained.to_dict()["additional_prior_user_requests_omitted"] == 3


def test_assistant_omitted_count_is_absent_when_zero() -> None:
    assert "prior_assistant_messages_omitted" not in _context(users={0: "u0"}, assistants={1: "a1"}).to_dict()


@pytest.mark.parametrize("count", [-1, True, 1.0])
def test_assistant_omitted_count_must_be_a_non_negative_exact_integer(count: Any) -> None:
    with pytest.raises(ValueError, match="prior_assistant_messages_omitted"):
        PlannerConversationContext(
            prior_user_requests=(PlannerPriorUserRequest(history_index=0, content="u0"),),
            prior_assistant_messages_omitted=count,
        )


@pytest.mark.parametrize("indices", [frozenset({7}), frozenset({0, 7})])
def test_without_refuses_indices_it_does_not_hold(indices: frozenset[int]) -> None:
    with pytest.raises(ValueError, match="history_indices"):
        _context(users={0: "u0"}).without(indices)


def test_the_largest_omission_counts_fit_the_context_budget_allowance() -> None:
    """Re-rendering after removing every entry adds no more than the allowance on the decision's measure."""
    largest = 2**53 - 1  # the largest integer canonical JSON encodes
    before = _context(users={0: "u" * 50}, assistants={1: "a" * 50})
    after = PlannerConversationContext(
        prior_user_requests=(), additional_prior_user_requests_omitted=largest, prior_assistant_messages_omitted=largest
    )
    savings = _entry_saving(0, "u" * 50) + _entry_saving(1, "a" * 50)

    def size(context: PlannerConversationContext) -> int:
        message = _planner_request_message({"intent": "Build it.", "conversation_context": context.to_dict()})
        return _planner_request_size([{"role": message["role"], "content": message["content"]}], [])

    assert size(after) - (size(before) - savings) <= _ALLOWANCE


# --- Conversation context at the call site ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_very_high_cap_sends_the_full_context_and_records_that_nothing_was_shed(tmp_path: Path, tool_context: ToolContext) -> None:
    context = _context(users={0: "Build CSV to JSON.", 2: "Keep every row."}, assistants={1: "x" * 5_000, 3: "y" * 5_000})

    request, call = await _decline(tmp_path, tool_context, context)

    assert _payload(request)["conversation_context"] == context.to_dict()
    budget = call.context_budget
    assert budget is not None
    assert budget.shed_history_indices == () and budget.stubbed_tool_call_ids == () and budget.shed_hatch_event_indices == ()
    assert budget.bytes_per_token == repr(_FALLBACK_RATIO)
    assert budget.estimated_tokens_before == budget.estimated_tokens_after
    assert budget.policy_hash == _context_policy().audit_hash


@pytest.mark.asyncio
async def test_assistant_prose_is_shed_before_user_requests(tmp_path: Path, tool_context: ToolContext) -> None:
    users = {0: "Build CSV to JSON.", 2: "Keep every row."}
    assistants = {1: "a" * 40_000, 3: "b" * 40_000}
    users_only_request, _ = await _decline(tmp_path, tool_context, _context(users=users))
    full_context = _context(users=users, assistants=assistants)
    full_request, _ = await _decline(tmp_path, tool_context, full_context)
    users_only_bytes = _sent_bytes(users_only_request)
    # The decision's input: the request as sent plus the omission allowance.
    full_bytes = _sent_bytes(full_request) + _ALLOWANCE
    low_bytes = users_only_bytes + _ALLOWANCE + 500
    policy = _policy_shedding_to(low_bytes)
    assert full_bytes / _FALLBACK_RATIO > policy.shed_above * policy.max_prompt_tokens  # over the high mark
    assert full_bytes - _entry_saving(1, assistants[1]) > low_bytes  # one removal is not enough

    request, call = await _decline(tmp_path, tool_context, full_context, policy=policy)

    sent = _payload(request)["conversation_context"]
    assert "prior_assistant_messages" not in sent
    assert sent["prior_assistant_messages_omitted"] == 2
    assert sent["prior_user_requests"] == [{"history_index": 0, "content": users[0]}, {"history_index": 2, "content": users[2]}]
    assert sent["additional_prior_user_requests_omitted"] == 0
    # Only the conversation context changed; the rest of the request and the other messages are as measured.
    assert {key: value for key, value in _payload(request).items() if key != "conversation_context"} == {
        key: value for key, value in _payload(full_request).items() if key != "conversation_context"
    }
    assert [message for index, message in enumerate(request["messages"]) if index != 2] == [
        message for index, message in enumerate(full_request["messages"]) if index != 2
    ]
    budget = call.context_budget
    assert budget is not None
    assert budget.shed_history_indices == (1, 3)
    assert budget.stubbed_tool_call_ids == ()
    assert budget.estimated_tokens_before == math.ceil(full_bytes / _FALLBACK_RATIO)
    assert budget.estimated_tokens_after == math.ceil(
        (full_bytes - _entry_saving(1, assistants[1]) - _entry_saving(3, assistants[3])) / _FALLBACK_RATIO
    )
    assert budget.policy_hash == policy.audit_hash


@pytest.mark.asyncio
@pytest.mark.parametrize(("keep", "expected_retained"), [(1, [2]), (0, [])], ids=["newest-retained", "none-retained"])
async def test_oldest_user_requests_are_shed_last_and_counted(
    tmp_path: Path, tool_context: ToolContext, keep: int, expected_retained: list[int]
) -> None:
    users = {0: "r" * 40_000, 1: "s" * 40_000, 2: "t" * 40_000}
    context = _context(users=users)
    full_request, _ = await _decline(tmp_path, tool_context, context)
    full_bytes = _sent_bytes(full_request) + _ALLOWANCE
    shed_saving = sum(_entry_saving(index, text) for index, text in users.items() if index not in expected_retained)
    policy = _policy_shedding_to(full_bytes - shed_saving + 100)
    assert full_bytes / _FALLBACK_RATIO > policy.shed_above * policy.max_prompt_tokens

    request, call = await _decline(tmp_path, tool_context, context, policy=policy)

    sent = _payload(request)["conversation_context"]
    assert sent["prior_user_requests"] == [{"history_index": index, "content": users[index]} for index in expected_retained]
    assert sent["additional_prior_user_requests_omitted"] == 3 - keep
    assert "prior_assistant_messages_omitted" not in sent
    budget = call.context_budget
    assert budget is not None
    assert budget.shed_history_indices == tuple(index for index in users if index not in expected_retained)


@pytest.mark.asyncio
async def test_history_shed_on_an_earlier_turn_stays_shed_between_the_marks(tmp_path: Path, tool_context: ToolContext) -> None:
    context = _context(users={0: "Build CSV to JSON."}, assistants={1: "a" * 20_000})
    full_request, _ = await _decline(tmp_path, tool_context, context)
    full_bytes = _sent_bytes(full_request) + _ALLOWANCE
    ratio = 4.0
    # The full request sits at 70% of the cap: under the high mark, over the low mark.
    policy = _context_policy(max_prompt_tokens=math.ceil(full_bytes / ratio / 0.7), max_request_bytes=1_000_000)

    fresh_request, fresh = await _decline(
        tmp_path,
        tool_context,
        context,
        policy=policy,
        history=ContextBudgetHistory(bytes_per_token=ratio, previously_shed_history=frozenset()),
    )
    sticky_request, sticky = await _decline(
        tmp_path,
        tool_context,
        context,
        policy=policy,
        history=ContextBudgetHistory(bytes_per_token=ratio, previously_shed_history=frozenset({"history:1"})),
    )

    assert _payload(fresh_request)["conversation_context"] == context.to_dict()
    assert fresh.context_budget is not None and fresh.context_budget.shed_history_indices == ()
    assert fresh.context_budget.bytes_per_token == repr(ratio)
    sticky_context = _payload(sticky_request)["conversation_context"]
    assert "prior_assistant_messages" not in sticky_context and sticky_context["prior_assistant_messages_omitted"] == 1
    assert sticky.context_budget is not None and sticky.context_budget.shed_history_indices == (1,)


async def _hatch_run(
    tmp_path: Path, tool_context: ToolContext, context: PlannerConversationContext, policy: ContextBudgetPolicy | None
) -> tuple[_ScriptedCompletion, BufferingRecorder]:
    completion = _ScriptedCompletion(
        _response_with_call_id("state", "get_pipeline_state", {}),
        _response_with_call_id("sources", "list_sources", {}),
        _text_response("Please upload the input for JSON conversion."),
    )
    recorder = BufferingRecorder()
    with pytest.raises(PlannerDeclined):
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            recorder=recorder,
            intent="The latter.",
            conversation_context=context,
            context_policy=policy,
            model_overrides={
                "model_identifier": _UNMARKED,
                "max_discovery_turns": 1,
                "escape_hatch_model": "test/advisor",
                "escape_hatch_provider": "test-provider",
            },
        )
    assert len(completion.requests) == len(recorder.llm_calls) == 3
    assert completion.requests[2]["model"] == "test/advisor"  # the third call is the escape hatch
    return completion, recorder


@pytest.mark.asyncio
async def test_the_escape_hatch_request_sheds_its_conversation_context_and_keeps_the_transcript(
    tmp_path: Path, tool_context: ToolContext
) -> None:
    assistants = {1: "a" * 40_000}
    context = _context(users={0: "Convert the data."}, assistants=assistants)
    measured_completion, measured_recorder = await _hatch_run(tmp_path, tool_context, context, None)
    first_bytes, _, hatch_bytes = (_sent_bytes(request) + _ALLOWANCE for request in measured_completion.requests)
    hatch_bytes += _TRANSCRIPT_ALLOWANCE
    saving = _entry_saving(1, assistants[1])
    policy = _policy_shedding_to(hatch_bytes - saving + 200, shed_to=0.8)
    # Both requests are over the high mark and fit once the assistant message is shed.
    assert first_bytes / _FALLBACK_RATIO > policy.shed_above * policy.max_prompt_tokens
    assert hatch_bytes / _FALLBACK_RATIO > policy.shed_above * policy.max_prompt_tokens

    completion, recorder = await _hatch_run(tmp_path, tool_context, context, policy)

    hatch_request = completion.requests[2]
    measured_hatch = measured_completion.requests[2]
    _sent_matches_record(hatch_request, recorder.llm_calls[2])
    sent = _payload(hatch_request)["conversation_context"]
    assert "prior_assistant_messages" not in sent and sent["prior_assistant_messages_omitted"] == 1
    assert sent["prior_user_requests"] == [{"history_index": 0, "content": "Convert the data."}]
    assert "prior_assistant_messages" in _payload(measured_hatch)["conversation_context"]
    # Shedding the assistant message is enough: the hatch transcript and everything after it are sent as measured.
    assert len(hatch_request["messages"]) == len(measured_hatch["messages"])
    assert hatch_request["messages"][3]["role"] == "user"
    assert _normalised(hatch_request["messages"][3]["content"], recorder) == _normalised(
        measured_hatch["messages"][3]["content"], measured_recorder
    )
    assert hatch_request["messages"][4] == measured_hatch["messages"][4]
    for call in recorder.llm_calls:
        assert call.context_budget is not None and call.context_budget.shed_history_indices == (1,)
        assert call.context_budget.shed_hatch_event_indices == ()


@pytest.mark.asyncio
async def test_a_re_rendered_request_keeps_the_state_it_was_first_sent_with(tmp_path: Path, tool_context: ToolContext) -> None:
    """Shedding re-renders the request as sent, never from the caller's live mappings."""
    context = _context(users={0: "Convert the data."}, assistants={1: "a" * 40_000})
    provider_state = _empty_state().to_dict()
    expected_state = deepcopy(provider_state)

    class MutatingCompletion(_ScriptedCompletion):
        async def __call__(self, **kwargs: Any) -> Any:
            if not self.requests:
                provider_state["version"] = 999
            return await super().__call__(**kwargs)

    def script(completion_type: type[_ScriptedCompletion]) -> _ScriptedCompletion:
        return completion_type(
            _response_with_call_id("state", "get_pipeline_state", {}),
            _text_response("Please upload the input for JSON conversion."),
        )

    async def run(completion: _ScriptedCompletion, policy: ContextBudgetPolicy | None) -> BufferingRecorder:
        recorder = BufferingRecorder()
        with pytest.raises(PlannerDeclined):
            await _plan(
                tmp_path=tmp_path,
                tool_context=tool_context,
                completion=completion,
                recorder=recorder,
                conversation_context=context,
                provider_current_state=provider_state,
                context_policy=policy,
                model_overrides={"model_identifier": _UNMARKED},
            )
        assert len(completion.requests) == len(recorder.llm_calls) == 2
        return recorder

    measured_completion = script(_ScriptedCompletion)
    await run(measured_completion, None)
    first_bytes, second_bytes = (_sent_bytes(request) + _ALLOWANCE for request in measured_completion.requests)
    policy = _policy_shedding_to(second_bytes - _entry_saving(1, "a" * 40_000) + 200, shed_to=0.8)
    assert first_bytes / _FALLBACK_RATIO > policy.shed_above * policy.max_prompt_tokens

    completion = script(MutatingCompletion)
    recorder = await run(completion, policy)

    assert provider_state["version"] == 999
    for call in recorder.llm_calls:
        assert call.context_budget is not None and call.context_budget.shed_history_indices == (1,)
    first, second = (request["messages"][2] for request in completion.requests)
    assert second == first
    assert _payload(completion.requests[1])["current_state"] == expected_state


# --- Tool results across calls ----------------------------------------------------------------


def _discovery_then_emit(tmp_path: Path, *, large_first: bool = True) -> _ScriptedCompletion:
    """Three schema lookups, then a proposal. The llm schema result is about twice the web_scrape one."""
    large = _response_with_call_id("schema-llm", "get_plugin_schema", {"plugin_type": "transform", "name": "llm"})
    small = _response_with_call_id("schema-scrape", "get_plugin_schema", {"plugin_type": "transform", "name": "web_scrape"})
    return _ScriptedCompletion(
        *((large, small) if large_first else (small, large)),
        _response_with_call_id("schema-csv", "get_plugin_schema", {"plugin_type": "source", "name": "csv"}),
        _response_with_call_id("emit", "emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)}),
    )


async def _discovery_run(
    tmp_path: Path, tool_context: ToolContext, *, model: str, policy: ContextBudgetPolicy | None, large_first: bool = True
) -> tuple[_ScriptedCompletion, BufferingRecorder]:
    completion = _discovery_then_emit(tmp_path, large_first=large_first)
    recorder = BufferingRecorder()
    await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        completion=completion,
        recorder=recorder,
        context_policy=policy,
        model_overrides={"model_identifier": model, "max_discovery_turns": 5},
    )
    assert len(completion.requests) == len(recorder.llm_calls) == 4
    return completion, recorder


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [_ANTHROPIC, _UNMARKED], ids=["anthropic-marked", "unmarked"])
async def test_earlier_tool_results_are_stubbed_and_the_latest_round_is_intact(
    tmp_path: Path, tool_context: ToolContext, model: str
) -> None:
    measured_completion, _ = await _discovery_run(tmp_path, tool_context, model=model, policy=None)
    # No conversation context, so no omission allowance.
    totals = [_sent_bytes(request) for request in measured_completion.requests]
    first_result = measured_completion.requests[3]["messages"][4]
    assert first_result["role"] == "tool" and first_result["tool_call_id"] == "schema-llm"
    first_saving = len(first_result["content"].encode("utf-8")) - len(CONTEXT_BUDGET_TOOL_RESULT_STUB.encode("utf-8"))
    # High mark between the third and fourth request; stubbing the first result reaches the low mark.
    shed_above, shed_to = 0.85, 0.8
    policy = _context_policy(
        max_prompt_tokens=math.floor((totals[2] + totals[3]) / 2 / _FALLBACK_RATIO / shed_above),
        max_request_bytes=1_000_000,
        shed_above=shed_above,
        shed_to=shed_to,
    )
    assert math.ceil(totals[2] / _FALLBACK_RATIO) <= shed_above * policy.max_prompt_tokens < math.ceil(totals[3] / _FALLBACK_RATIO)
    assert (totals[3] - first_saving) / _FALLBACK_RATIO <= shed_to * policy.max_prompt_tokens

    completion, recorder = await _discovery_run(tmp_path, tool_context, model=model, policy=policy)

    for request, call in zip(completion.requests, recorder.llm_calls, strict=True):
        _sent_matches_record(request, call)
    for ordinal, call in enumerate(recorder.llm_calls[:3]):
        assert call.context_budget is not None, ordinal
        assert call.context_budget.stubbed_tool_call_ids == () and call.context_budget.shed_history_indices == (), ordinal
        assert completion.requests[ordinal]["messages"] == measured_completion.requests[ordinal]["messages"], ordinal
    last_budget = recorder.llm_calls[3].context_budget
    assert last_budget is not None
    assert last_budget.stubbed_tool_call_ids == ("schema-llm",)
    assert last_budget.shed_history_indices == ()

    sent = completion.requests[3]["messages"]
    unshed = measured_completion.requests[3]["messages"]
    stubbed = sent[4]
    assert stubbed["role"] == "tool" and stubbed["tool_call_id"] == "schema-llm"
    assert stubbed["content"] == CONTEXT_BUDGET_TOOL_RESULT_STUB
    # Everything else, including the second result and the latest round trip, is unchanged.
    assert [message for index, message in enumerate(sent) if index != 4] == [message for index, message in enumerate(unshed) if index != 4]
    assert sent[7]["tool_call_id"] == "schema-scrape" and sent[7]["content"] != CONTEXT_BUDGET_TOOL_RESULT_STUB
    assert sent[9]["role"] == "assistant" and sent[10]["tool_call_id"] == "schema-csv"
    assert sent[10]["content"] != CONTEXT_BUDGET_TOOL_RESULT_STUB

    # The cached prefix (skill, reference block, tools) is byte-identical on every request of the shedding run.
    for request in completion.requests:
        assert canonical_json(request["messages"][:2]) == canonical_json(completion.requests[0]["messages"][:2])
        assert canonical_json(request["tools"]) == canonical_json(completion.requests[0]["tools"])
    if model == _ANTHROPIC:
        assert sent[0]["cache_control"] == {"type": "ephemeral"}
        assert completion.requests[3]["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    else:
        assert "cache_control" not in sent[0]


# --- Refusals ---------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("ceiling", ["tokens", "bytes"])
async def test_an_oversized_intent_is_refused_before_any_provider_call(tmp_path: Path, tool_context: ToolContext, ceiling: str) -> None:
    small_request, _ = await _decline(tmp_path, tool_context, _context(users={0: "Build CSV to JSON."}))
    base_bytes = _sent_bytes(small_request)
    cap_bytes = base_bytes + 10_000
    policy = (
        _context_policy(max_prompt_tokens=math.ceil(cap_bytes / _FALLBACK_RATIO), max_request_bytes=1_000_000)
        if ceiling == "tokens"
        else _context_policy(max_request_bytes=cap_bytes)
    )
    completion = _ScriptedCompletion(_text_response("Must not dispatch."))
    recorder = BufferingRecorder()

    with pytest.raises(PipelinePlannerError) as caught:
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            recorder=recorder,
            intent="Build this: " + "z" * 150_000,
            conversation_context=_context(users={0: "Build CSV to JSON."}),
            context_policy=policy,
        )

    assert caught.value.code == ComposerPlannerCode.CONTEXT_BUDGET_EXHAUSTED.value
    assert caught.value.detail_codes == (f"context_ceiling_{ceiling}", "largest_component_request")
    assert completion.requests == []
    assert len(completion._responses) == 1  # no scripted response consumed
    assert recorder.llm_calls == ()


async def _decline_or_refusal(
    tmp_path: Path,
    tool_context: ToolContext,
    context: PlannerConversationContext,
    *,
    policy: ContextBudgetPolicy,
    model: str,
) -> tuple[_ScriptedCompletion, BufferingRecorder, PipelinePlannerError]:
    """One planner call under ``policy``; the planner's byte budget is the same ceiling, as in production."""
    completion = _ScriptedCompletion(_text_response("Which output format do you want?"))
    recorder = BufferingRecorder()
    with pytest.raises(PipelinePlannerError) as caught:
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            recorder=recorder,
            conversation_context=context,
            context_policy=policy,
            budget=_budget(max_request_bytes=policy.max_request_bytes),
            model_overrides={"model_identifier": model},
        )
    return completion, recorder, caught.value


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [_ANTHROPIC, _UNMARKED], ids=["anthropic-marked", "unmarked"])
async def test_a_request_within_the_re_render_margin_of_the_ceiling_sheds_one_more_segment_and_is_sent(
    tmp_path: Path, tool_context: ToolContext, model: str
) -> None:
    """Shedding the first assistant message lands a few bytes under the ceiling before the re-render.

    The re-rendered omission count (and, for Anthropic, the cache markers the
    decision measured) would push it back over, so the decision sheds the
    next assistant message too and the request is sent at or under the
    ceiling. Nothing protected is anywhere near the ceiling.
    """
    assistants = {1: "a" * 20_000, 2: "b" * 5_000}
    context = _context(users={0: "Build CSV to JSON."}, assistants=assistants)
    full_request, _ = await _decline(tmp_path, tool_context, context, model=model)
    cap_bytes = _sent_bytes(full_request) - _entry_saving(1, assistants[1]) + 10
    policy = _context_policy(max_request_bytes=cap_bytes)

    completion, recorder, outcome = await _decline_or_refusal(tmp_path, tool_context, context, policy=policy, model=model)

    assert type(outcome) is PlannerDeclined
    (request,) = completion.requests
    (call,) = recorder.llm_calls
    _sent_matches_record(request, call)
    assert _sent_bytes(request) <= cap_bytes
    assert call.request_byte_split.total_bytes <= cap_bytes
    sent = _payload(request)["conversation_context"]
    assert "prior_assistant_messages" not in sent and sent["prior_assistant_messages_omitted"] == 2
    assert sent["prior_user_requests"] == [{"history_index": 0, "content": "Build CSV to JSON."}]
    assert call.context_budget is not None
    assert call.context_budget.shed_history_indices == (1, 2)
    assert call.context_budget.estimated_tokens_after <= policy.max_prompt_tokens


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [_ANTHROPIC, _UNMARKED], ids=["anthropic-marked", "unmarked"])
async def test_a_dispatched_request_never_exceeds_either_ceiling(tmp_path: Path, tool_context: ToolContext, model: str) -> None:
    """Over ceilings straddling the size after one shed, every request is sent within both ceilings or refused unsent."""
    assistants = {1: "a" * 20_000, 2: "b" * 5_000}
    context = _context(users={0: "Build CSV to JSON."}, assistants=assistants)
    full_request, _ = await _decline(tmp_path, tool_context, context, model=model)
    one_shed = _sent_bytes(full_request) - _entry_saving(1, assistants[1])
    policies = [_context_policy(max_request_bytes=one_shed + delta) for delta in (-6_000, -400, -10, 0, 10, 100, 400)]
    policies += [
        _context_policy(max_prompt_tokens=math.ceil((one_shed + delta) / _FALLBACK_RATIO), max_request_bytes=1_000_000)
        for delta in (-6_000, -400, -10, 0, 10, 100, 400)
    ]
    sent_count = 0
    for policy in policies:
        completion, recorder, outcome = await _decline_or_refusal(tmp_path, tool_context, context, policy=policy, model=model)
        if type(outcome) is PlannerDeclined:
            sent_count += 1
            (request,) = completion.requests
            (call,) = recorder.llm_calls
            assert _sent_bytes(request) <= policy.max_request_bytes, policy
            assert call.request_byte_split.total_bytes <= policy.max_request_bytes, policy
            assert call.context_budget is not None
            assert call.context_budget.estimated_tokens_after <= policy.max_prompt_tokens, policy
        else:
            # Only the budget may refuse, and only before dispatch.
            assert outcome.code == "CONTEXT_BUDGET_EXHAUSTED", (policy, outcome.code)
            assert completion.requests == [] and recorder.llm_calls == ()
    assert sent_count >= len(policies) - 2  # the two tightest ceilings may refuse; the rest are sent


@pytest.mark.asyncio
async def test_a_later_call_whose_protected_content_cannot_fit_is_refused(tmp_path: Path, tool_context: ToolContext) -> None:
    """Earlier calls are sent; the refused call is never dispatched and leaves no call record."""
    measured_completion, _ = await _discovery_run(tmp_path, tool_context, model=_UNMARKED, policy=None, large_first=False)
    # The second request is admitted; the third cannot fit even with the first result stubbed,
    # because its latest round (the larger result) is protected.
    totals = [_sent_bytes(request) for request in measured_completion.requests]
    protected_third = totals[2] - (
        len(measured_completion.requests[2]["messages"][4]["content"].encode("utf-8"))
        - len(CONTEXT_BUDGET_TOOL_RESULT_STUB.encode("utf-8"))
    )
    cap_bytes = (totals[1] + protected_third) // 2
    assert totals[1] <= cap_bytes < protected_third
    completion = _discovery_then_emit(tmp_path, large_first=False)
    recorder = BufferingRecorder()

    with pytest.raises(PipelinePlannerError) as caught:
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            recorder=recorder,
            context_policy=_context_policy(max_request_bytes=cap_bytes),
            model_overrides={"model_identifier": _UNMARKED, "max_discovery_turns": 5},
        )

    assert caught.value.code == "CONTEXT_BUDGET_EXHAUSTED"
    assert caught.value.detail_codes == ("context_ceiling_bytes", "largest_component_reference")
    assert len(completion.requests) == len(recorder.llm_calls) == 2


def test_a_context_budget_refusal_settles_an_open_attempt_as_budget_exhaustion() -> None:
    recorder = BufferingRecorder()
    trail = _PlannerAttemptTrail(session_id="session", operation_id=None, surface="freeform", recorder=recorder)
    trail.begin_attempt(planner_call_ordinal=1, phase_hint=ComposerPlannerAttemptPhase.RESPONSE)

    trail.finalize_active_exception(
        PipelinePlannerError(
            "planner context budget exhausted",
            code="CONTEXT_BUDGET_EXHAUSTED",
            detail_codes=("context_ceiling_tokens", "largest_component_request"),
        )
    )

    (attempt,) = recorder.planner_attempts
    assert attempt.outcome is ComposerPlannerAttemptOutcome.BUDGET_EXHAUSTED
    assert attempt.planner_code is ComposerPlannerCode.CONTEXT_BUDGET_EXHAUSTED


# --- The escape hatch's transcript ------------------------------------------------------------


def _normalised(content: str, recorder: BufferingRecorder) -> str:
    """A run's text with its physical call ids replaced, so two runs' transcripts compare by content."""
    for index, call in enumerate(recorder.llm_calls):
        content = content.replace(call.call_id, f"physical-call-{index}")
    return content


def _transcript_of(content: str) -> AdvisorVisibleTranscript:
    """The transcript a hatch message renders, rebuilt from the message content."""
    assert content.startswith(_HATCH_TRANSCRIPT_HEADER)
    return AdvisorVisibleTranscript("", "binding", content.removeprefix(_HATCH_TRANSCRIPT_HEADER), "semantic")


def _hatch_events(request: dict[str, Any]) -> list[dict[str, Any]]:
    events = json.loads(_transcript_of(request["messages"][3]["content"]).provider_json)["events"]
    assert type(events) is list
    return events


@pytest.mark.asyncio
async def test_the_escape_hatch_sheds_its_oldest_transcript_events_after_assistant_prose(tmp_path: Path, tool_context: ToolContext) -> None:
    assistants = {1: "a" * 40_000}
    context = _context(users={0: "Convert the data."}, assistants=assistants)
    measured_completion, measured_recorder = await _hatch_run(tmp_path, tool_context, context, None)
    measured_hatch = measured_completion.requests[2]
    measured_transcript = _transcript_of(_normalised(measured_hatch["messages"][3]["content"], measured_recorder))
    sheddable = measured_transcript.sheddable_event_indices()
    event_sizes = measured_transcript.event_bytes()
    assert len(sheddable) >= 3
    hatch_bytes = _sent_bytes(measured_hatch) + _ALLOWANCE + _TRANSCRIPT_ALLOWANCE
    # Shedding the assistant message and the two oldest events lands under the low mark; one fewer does not.
    target = hatch_bytes - _entry_saving(1, assistants[1]) - event_sizes[sheddable[0]] - event_sizes[sheddable[1]]
    policy = _policy_shedding_to(target + 30, shed_to=0.8)
    assert hatch_bytes / _FALLBACK_RATIO > policy.shed_above * policy.max_prompt_tokens

    completion, recorder = await _hatch_run(tmp_path, tool_context, context, policy)

    hatch_request = completion.requests[2]
    hatch_call = recorder.llm_calls[2]
    _sent_matches_record(hatch_request, hatch_call)
    assert hatch_call.context_budget is not None
    assert hatch_call.context_budget.shed_history_indices == (1,)
    assert hatch_call.context_budget.shed_hatch_event_indices == sheddable[:2]
    # The user request was not shed: hatch events go before it.
    assert _payload(hatch_request)["conversation_context"]["prior_user_requests"] == [{"history_index": 0, "content": "Convert the data."}]
    # The sent transcript is the measured one without its two oldest events, byte for byte.
    sent_content = _normalised(hatch_request["messages"][3]["content"], recorder)
    expected = measured_transcript.without_events(frozenset(sheddable[:2]))
    assert sent_content == _HATCH_TRANSCRIPT_HEADER + expected.provider_json
    payload = json.loads(expected.provider_json)
    assert payload["events"] == json.loads(measured_transcript.provider_json)["events"][2:]
    assert payload["availability_gaps"][-1] == CONTEXT_BUDGET_TRANSCRIPT_OMISSION_GAP
    # Nothing else in the hatch request moved.
    assert len(hatch_request["messages"]) == len(measured_hatch["messages"])
    assert hatch_request["messages"][4] == measured_hatch["messages"][4]
    # The calls before the hatch carry no transcript.
    for call in recorder.llm_calls[:2]:
        assert call.context_budget is not None and call.context_budget.shed_hatch_event_indices == ()


def _hatch_message(events: list[Any], gaps: list[str]) -> tuple[AdvisorVisibleTranscript, dict[str, Any]]:
    payload = {"events": events, "availability_gaps": gaps, "current_request_ref": "initial_planner_request.intent", "scope": "s"}
    transcript = AdvisorVisibleTranscript("", "binding", json.dumps(payload, ensure_ascii=False), "semantic")
    return transcript, _hatch_transcript_message(transcript)


def _request_size(message: dict[str, Any]) -> int:
    return _planner_request_size(detach_prompt_guard_evidence([message]).messages, [])


_AWKWARD_EVENTS: list[Any] = [
    {"role": "user", "content": 'quotes " and \\ backslashes \\" and \n newlines \t tabs \u0001'},
    {"role": "tool", "content": "non-ASCII \u00e9\u4e2d\U0001f600 and line separators \u2028\u2029"},
    {"role": "assistant", "number": 1e20, "small": 1e-7, "nested": {"list": [1, 2.5, None, True]}},
    {"content_ref": "initial_planner_request.intent", "role": "user"},
    {"role": "tool", "content": '{"success": true}'},
]


@pytest.mark.parametrize("gaps", [[], ["authenticated_persisted_history_not_supplied"]], ids=["no-gaps", "a-gap"])
@pytest.mark.parametrize(
    "events",
    [_AWKWARD_EVENTS, _AWKWARD_EVENTS[:1], [_AWKWARD_EVENTS[2], _AWKWARD_EVENTS[3]]],
    ids=["mixed", "single-event", "reference-last"],
)
def test_transcript_event_savings_with_the_gap_allowance_never_exceed_the_bytes_shedding_saves(events: list[Any], gaps: list[str]) -> None:
    """For every prefix the budget can shed, counted savings minus the allowance are at most the bytes actually saved."""
    transcript, message = _hatch_message(events, gaps)
    full = _request_size(message)
    sheddable = transcript.sheddable_event_indices()
    sizes = transcript.event_bytes()
    assert sheddable
    for count in range(1, len(sheddable) + 1):
        shed = sheddable[:count]
        reduced = _request_size(_hatch_transcript_message(transcript.without_events(frozenset(shed))))
        counted = sum(sizes[index] for index in shed)
        assert counted <= full - reduced + _TRANSCRIPT_ALLOWANCE, (shed, counted, full - reduced)


def test_the_transcript_gap_allowance_is_needed() -> None:
    """Negative control: without the allowance, shedding a lone event over-counts its saving."""
    transcript, message = _hatch_message(_AWKWARD_EVENTS[:1], [])
    reduced = _request_size(_hatch_transcript_message(transcript.without_events(frozenset({0}))))

    assert transcript.event_bytes()[0] > _request_size(message) - reduced


def test_a_refusal_names_the_transcript_without_the_events_that_could_still_be_shed() -> None:
    transcript, message = _hatch_message(_AWKWARD_EVENTS, [])
    sizes = transcript.event_bytes()
    hatch_sheddable = sum(sizes[index] for index in transcript.sheddable_event_indices())
    content_bytes = len(message["content"].encode("utf-8"))
    split = ComposerLLMRequestByteSplit(message_bytes=(10, 20, 30, content_bytes), tools_bytes=5, total_bytes=65 + content_bytes)

    named = _planner_context_budget_components(
        split, latest_start=None, hatch_transcript_index=3, history_sheddable=0, tool_sheddable=0, hatch_sheddable=hatch_sheddable
    )

    assert named["transcript"] == content_bytes - hatch_sheddable
    assert named["other"] == 0
    assert largest_component(named) == "transcript"


def _fit_hatch_request(
    transcript: AdvisorVisibleTranscript, *, earlier_calls: tuple[ComposerLLMCall, ...], policy: ContextBudgetPolicy | None = None
) -> tuple[list[dict[str, Any]], ComposerContextBudgetRecord, ContextBudgetPolicy]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "skill"},
        {"role": "user", "content": "reference"},
        _planner_request_message({"intent": "Convert the data."}),
        _hatch_transcript_message(transcript),
        {"role": "user", "content": "notice"},
    ]
    if policy is None:
        # Between the marks: the request is not over the high mark, but above the low mark.
        size = _request_size_of(messages) + _TRANSCRIPT_ALLOWANCE
        policy = _context_policy(max_prompt_tokens=math.ceil(size / _FALLBACK_RATIO / 0.7), max_request_bytes=1_000_000)
        assert policy.shed_to * policy.max_prompt_tokens < size / _FALLBACK_RATIO <= policy.shed_above * policy.max_prompt_tokens
    before = deepcopy(messages)
    applied, record = _fit_planner_context_budget(
        messages=messages,
        tools=[],
        request_conversation_context=None,
        hatch_transcript_index=3,
        hatch_transcript=transcript,
        policy=policy,
        history=ContextBudgetHistory.empty(),
        earlier_calls=earlier_calls,
        mark_for_cache=False,
    )
    assert messages == before  # the caller's messages are never shed in place
    return applied, record, policy


def _request_size_of(messages: list[dict[str, Any]]) -> int:
    return _planner_request_size(detach_prompt_guard_evidence(messages).messages, [])


def _earlier_call(shed_keys: tuple[str, ...]) -> ComposerLLMCall:
    return build_llm_call_record(
        model_requested="test/advisor",
        messages=[{"role": "system", "content": "skill"}],
        tools=None,
        status=ComposerLLMCallStatus.SUCCESS,
        started_at=datetime.now(UTC),
        started_ns=time.monotonic_ns(),
        temperature=None,
        seed=None,
        response=None,
        context_budget=ComposerContextBudgetRecord.from_shed_keys(
            policy_hash="a" * 64, bytes_per_token=3.0, estimated_tokens_before=2, estimated_tokens_after=1, shed_keys=shed_keys
        ),
    )


def test_transcript_events_shed_on_an_earlier_call_of_the_request_stay_shed_between_the_marks() -> None:
    transcript, _ = _hatch_message(_AWKWARD_EVENTS, ["authenticated_persisted_history_not_supplied"])

    fresh, fresh_record, policy = _fit_hatch_request(transcript, earlier_calls=())
    sticky, sticky_record, _ = _fit_hatch_request(transcript, earlier_calls=(_earlier_call(("hatch:0", "hatch:1")),), policy=policy)

    assert fresh_record.shed_hatch_event_indices == ()
    assert fresh[3]["content"] == _HATCH_TRANSCRIPT_HEADER + transcript.provider_json
    assert sticky_record.shed_hatch_event_indices == (0, 1)
    assert sticky[3]["content"] == _HATCH_TRANSCRIPT_HEADER + transcript.without_events(frozenset({0, 1})).provider_json
    # The rebuilt message's prompt-guard parts describe the content it now carries.
    assert "".join(part.text for part in sticky[3][PROMPT_GUARD_CONTENT_PARTS_KEY]) == sticky[3]["content"]


def test_a_hatch_message_that_is_not_the_labelled_transcript_is_a_framework_bug() -> None:
    transcript, _ = _hatch_message(_AWKWARD_EVENTS, [])
    other, _ = _hatch_message(_AWKWARD_EVENTS[1:], [])
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "skill"},
        {"role": "user", "content": "reference"},
        _planner_request_message({"intent": "Convert the data."}),
        _hatch_transcript_message(other),
    ]

    with pytest.raises(FrameworkBugError, match="transcript"):
        _fit_planner_context_budget(
            messages=messages,
            tools=[],
            request_conversation_context=None,
            hatch_transcript_index=3,
            hatch_transcript=transcript,
            policy=_context_policy(max_request_bytes=1_000_000),
            history=ContextBudgetHistory.empty(),
            earlier_calls=(),
            mark_for_cache=False,
        )


def _context_budget_refusal(outcome: PipelinePlannerError) -> PipelinePlannerError:
    """The context budget refusal that ended a run, directly or as the context of the hatch's original exhaustion."""
    if outcome.code == "CONTEXT_BUDGET_EXHAUSTED":
        return outcome
    refusal = outcome.__context__
    assert type(refusal) is PipelinePlannerError and refusal.code == "CONTEXT_BUDGET_EXHAUSTED", (outcome.code, refusal)
    return refusal


async def _hatch_run_or_refusal(
    tmp_path: Path, tool_context: ToolContext, policy: ContextBudgetPolicy
) -> tuple[_ScriptedCompletion, BufferingRecorder, PipelinePlannerError]:
    """A hatch run under ``policy``; the planner's byte budget is the same ceiling, as in production."""
    completion = _ScriptedCompletion(
        _response_with_call_id("state", "get_pipeline_state", {}),
        _response_with_call_id("sources", "list_sources", {}),
        _text_response("Please upload the input for JSON conversion."),
    )
    recorder = BufferingRecorder()
    with pytest.raises(PipelinePlannerError) as caught:
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            recorder=recorder,
            intent="The latter.",
            context_policy=policy,
            budget=_budget(max_request_bytes=policy.max_request_bytes),
            model_overrides={
                "model_identifier": _UNMARKED,
                "max_discovery_turns": 1,
                "escape_hatch_model": "test/advisor",
                "escape_hatch_provider": "test-provider",
            },
        )
    return completion, recorder, caught.value


@pytest.mark.asyncio
async def test_a_dispatched_hatch_request_never_exceeds_either_ceiling(tmp_path: Path, tool_context: ToolContext) -> None:
    """Over ceilings straddling the hatch request after one event is shed, every request is sent within both ceilings or refused unsent."""
    measured_completion, _ = await _hatch_run(tmp_path, tool_context, None, None)
    measured_hatch = measured_completion.requests[2]
    transcript = _transcript_of(measured_hatch["messages"][3]["content"])
    one_shed = _sent_bytes(measured_hatch) - transcript.event_bytes()[transcript.sheddable_event_indices()[0]]
    deltas = (-2_000, -400, -10, 0, 10, 100, 400)
    policies = [_context_policy(max_request_bytes=one_shed + delta) for delta in deltas]
    policies += [
        _context_policy(max_prompt_tokens=math.ceil((one_shed + delta) / _FALLBACK_RATIO), max_request_bytes=1_000_000) for delta in deltas
    ]
    hatch_sent = 0
    hatch_shed = 0
    for policy in policies:
        completion, recorder, outcome = await _hatch_run_or_refusal(tmp_path, tool_context, policy)
        # Every dispatched request is the recorded one and fits both ceilings.
        assert len(completion.requests) == len(recorder.llm_calls)
        for request, call in zip(completion.requests, recorder.llm_calls, strict=True):
            _sent_matches_record(request, call)
            assert _sent_bytes(request) <= policy.max_request_bytes, policy
            assert call.context_budget is not None
            assert call.context_budget.estimated_tokens_after <= policy.max_prompt_tokens, policy
        if type(outcome) is PlannerDeclined:
            hatch_sent += 1
            assert completion.requests[2]["model"] == "test/advisor"
            hatch_record = recorder.llm_calls[2].context_budget
            assert hatch_record is not None
            shed_events = hatch_record.shed_hatch_event_indices
            # Oldest first: what is shed is always a prefix of the sheddable events, so the rest stays contiguous.
            assert shed_events == transcript.sheddable_event_indices()[: len(shed_events)]
            hatch_shed += bool(shed_events)
        else:
            # Only the budget may refuse, and only before dispatch.
            _context_budget_refusal(outcome)
    assert hatch_sent >= len(policies) - 2  # the two tightest ceilings may refuse; the rest are sent
    assert hatch_shed >= 4  # the ceilings below the full hatch request shed transcript events to send it


@pytest.mark.asyncio
async def test_a_hatch_request_whose_protected_content_cannot_fit_is_refused_after_shedding_every_event(
    tmp_path: Path, tool_context: ToolContext
) -> None:
    """The calls before the hatch are sent; the hatch call cannot fit with its whole transcript shed and is not dispatched.

    The refusal names the largest protected component. With every event shed
    the transcript is its header and envelope (plus any content-referenced
    event), far smaller than the catalog reference block, so it is the
    reference that is named here.
    """
    measured_completion, _ = await _hatch_run(tmp_path, tool_context, None, None)
    measured_hatch = measured_completion.requests[2]
    transcript = _transcript_of(measured_hatch["messages"][3]["content"])
    emptied = transcript.without_events(frozenset(transcript.sheddable_event_indices()))
    assert json.loads(emptied.provider_json)["events"] == []
    protected_hatch = _sent_bytes(
        {
            **measured_hatch,
            "messages": [
                *measured_hatch["messages"][:3],
                {"role": "user", "content": _HATCH_TRANSCRIPT_HEADER + emptied.provider_json},
                *measured_hatch["messages"][4:],
            ],
        }
    )
    earlier = max(_sent_bytes(request) for request in measured_completion.requests[:2])
    cap_bytes = (earlier + protected_hatch) // 2
    assert earlier <= cap_bytes < protected_hatch

    completion, recorder, outcome = await _hatch_run_or_refusal(tmp_path, tool_context, _context_policy(max_request_bytes=cap_bytes))

    # A failed hatch turn reports the exhaustion that engaged the hatch; the refusal is its context.
    assert outcome.code == "DISCOVERY_EXHAUSTED"
    refusal = _context_budget_refusal(outcome)
    assert refusal.detail_codes == ("context_ceiling_bytes", "largest_component_reference")
    assert len(completion.requests) == len(recorder.llm_calls) == 2
