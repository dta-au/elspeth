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
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.composer_planner_audit import ComposerPlannerAttemptOutcome, ComposerPlannerAttemptPhase, ComposerPlannerCode
from elspeth.core.canonical import canonical_json
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.context_budget import CONTEXT_BUDGET_TOOL_RESULT_STUB, ContextBudgetPolicy
from elspeth.web.composer.context_budget_history import ContextBudgetHistory
from elspeth.web.composer.llm_response_parsing import _hash_and_measure_request
from elspeth.web.composer.pipeline_planner import (
    PipelinePlannerError,
    PlannerConversationContext,
    PlannerDeclined,
    PlannerPriorAssistantMessage,
    PlannerPriorUserRequest,
    _PlannerAttemptTrail,
)
from elspeth.web.composer.tools import ToolContext
from tests.unit.web.composer.test_pipeline_planner import (
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


def _entry_saving(index: int, content: str) -> int:
    """Bytes an entry occupies inside the request message, plus its separator."""
    return len(canonical_json({"history_index": index, "content": content}).encode("utf-8")) + 1


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
    _, users_only = await _decline(tmp_path, tool_context, _context(users=users))
    full_context = _context(users=users, assistants=assistants)
    full_request, full = await _decline(tmp_path, tool_context, full_context)
    users_only_bytes = users_only.request_byte_split.total_bytes
    full_bytes = full.request_byte_split.total_bytes
    policy = _policy_shedding_to(users_only_bytes + 200)
    assert full_bytes / _FALLBACK_RATIO > policy.shed_above * policy.max_prompt_tokens  # over the high mark
    assert full_bytes - _entry_saving(1, assistants[1]) > (users_only_bytes + 200)  # one removal is not enough

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
    _, full = await _decline(tmp_path, tool_context, context)
    full_bytes = full.request_byte_split.total_bytes
    base_bytes = full_bytes - sum(_entry_saving(index, text) for index, text in users.items())
    policy = _policy_shedding_to(base_bytes + sum(_entry_saving(index, users[index]) for index in expected_retained) + 100)
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
    _, full = await _decline(tmp_path, tool_context, context)
    full_bytes = full.request_byte_split.total_bytes
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
    measured_completion, measured = await _hatch_run(tmp_path, tool_context, context, None)
    first_bytes, _, hatch_bytes = (call.request_byte_split.total_bytes for call in measured.llm_calls)
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
    # The hatch transcript and everything after it are not touched by this budget.
    assert len(hatch_request["messages"]) == len(measured_hatch["messages"])
    assert hatch_request["messages"][3]["role"] == "user"
    assert len(hatch_request["messages"][3]["content"]) == len(measured_hatch["messages"][3]["content"])
    for call in recorder.llm_calls:
        assert call.context_budget is not None and call.context_budget.shed_history_indices == (1,)


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

    measured = await run(script(_ScriptedCompletion), None)
    first_bytes, second_bytes = (call.request_byte_split.total_bytes for call in measured.llm_calls)
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
    measured_completion, measured = await _discovery_run(tmp_path, tool_context, model=model, policy=None)
    totals = [call.request_byte_split.total_bytes for call in measured.llm_calls]
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
    _, small = await _decline(tmp_path, tool_context, _context(users={0: "Build CSV to JSON."}))
    base_bytes = small.request_byte_split.total_bytes
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


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [_ANTHROPIC, _UNMARKED], ids=["anthropic-marked", "unmarked"])
async def test_an_applied_request_over_the_byte_ceiling_is_refused_before_any_provider_call(
    tmp_path: Path, tool_context: ToolContext, model: str
) -> None:
    """The decision admits the request; re-rendering the omission count pushes it back over."""
    assistants = {1: "a" * 20_000, 2: "b" * 150_000}
    context = _context(users={0: "Build CSV to JSON."}, assistants=assistants)
    _, full = await _decline(tmp_path, tool_context, context, model=_UNMARKED)
    full_bytes = full.request_byte_split.total_bytes
    cap_bytes = full_bytes - _entry_saving(1, assistants[1]) + 10
    completion = _ScriptedCompletion(_text_response("Must not dispatch."))
    recorder = BufferingRecorder()

    with pytest.raises(PipelinePlannerError) as caught:
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            recorder=recorder,
            conversation_context=context,
            context_policy=_context_policy(max_request_bytes=cap_bytes),
            model_overrides={"model_identifier": model},
        )

    assert caught.value.code == ComposerPlannerCode.CONTEXT_BUDGET_EXHAUSTED.value
    assert caught.value.detail_codes == ("context_ceiling_bytes", "largest_component_request")
    assert completion.requests == []
    assert recorder.llm_calls == ()


@pytest.mark.asyncio
async def test_a_later_call_whose_protected_content_cannot_fit_is_refused(tmp_path: Path, tool_context: ToolContext) -> None:
    """Earlier calls are sent; the refused call is never dispatched and leaves no call record."""
    measured_completion, measured = await _discovery_run(tmp_path, tool_context, model=_UNMARKED, policy=None, large_first=False)
    # The second request is admitted; the third cannot fit even with the first result stubbed,
    # because its latest round (the larger result) is protected.
    totals = [call.request_byte_split.total_bytes for call in measured.llm_calls]
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
