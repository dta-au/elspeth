"""A tool result the context budget stubbed must not cost the request its private capture.

The stub is the one server-authored tool result that is not JSON. Private
capture admits exactly that constant and nothing looser, so the request sent
stays retained and the next budget decision anchors on the call that carried
the stub. The compose-loop tests drive the real loop, recorder and anchor;
since no result of the turn in progress is stubbed, they now show the loop
sending every result whole while the anchor still advances.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from typing import Any

import pytest

from elspeth.contracts.prompt_guard import detach_prompt_guard_messages
from elspeth.core.canonical import canonical_json
from elspeth.web.composer.context_budget import (
    CONTEXT_BUDGET_TOOL_RESULT_STUB,
    ContextBudgetPolicy,
    SheddableKind,
    SheddableSegment,
    decide_context_budget,
)
from elspeth.web.composer.context_budget_apply import message_bytes, request_wire_bytes, stubbed_tool_message
from elspeth.web.composer.context_budget_history import request_anchor
from elspeth.web.composer.provider_context import capture_provider_context
from tests.unit.web.composer.test_compose_loop_context_budget import (
    _advisor_end_gate_clean as _advisor_end_gate_clean_fixture,
)
from tests.unit.web.composer.test_compose_loop_context_budget import (
    _base_request_bytes,
    _cap_for,
    _compose,
    _make_llm_response,
    _prepare_reply,
    _RecordingProvider,
    _service,
    _tool_turn,
    _two_round_turn,
    _wire_bytes,
)
from tests.unit.web.composer.test_planner_context_budget import _earlier_call
from tests.unit.web.composer.test_provider_context import _messages, _tools

_advisor_end_gate_clean = _advisor_end_gate_clean_fixture

_TOOL_RESULT_INDEX = 3


def _with_result(content: object, *, arguments: str = "{}") -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = _messages(arguments, '{"rows":"original result text"}')
    assert messages[_TOOL_RESULT_INDEX]["role"] == "tool"
    messages[_TOOL_RESULT_INDEX]["content"] = content
    return messages


def _stubbed_request() -> list[dict[str, Any]]:
    """The fixture request after the real stub producer replaced its tool result."""
    messages: list[dict[str, Any]] = _messages("{}", '{"rows":"original result text"}')
    messages[_TOOL_RESULT_INDEX] = stubbed_tool_message(messages[_TOOL_RESULT_INDEX])
    return messages


def test_a_request_with_a_stubbed_tool_result_is_retained() -> None:
    context = capture_provider_context(messages=_stubbed_request(), tools=_tools())

    assert context.disposition == "retained"
    assert context.reasons == ()
    assert context.canonical is not None


def test_the_retained_request_shows_the_stub_that_was_sent_and_not_the_original_result() -> None:
    messages = _stubbed_request()

    context = capture_provider_context(messages=messages, tools=_tools())

    assert context.canonical is not None
    retained = json.loads(context.canonical)
    # The retained request is the request sent, message for message: the
    # stubbed result reads as the stub text itself, verbatim and unquoted.
    assert retained["messages"] == messages
    assert retained["messages"][_TOOL_RESULT_INDEX] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": CONTEXT_BUDGET_TOOL_RESULT_STUB,
    }
    assert "original result text" not in context.canonical


@pytest.mark.parametrize(
    "content",
    [
        "not JSON",
        CONTEXT_BUDGET_TOOL_RESULT_STUB + " ",
        CONTEXT_BUDGET_TOOL_RESULT_STUB + "\n",
        " " + CONTEXT_BUDGET_TOOL_RESULT_STUB,
        CONTEXT_BUDGET_TOOL_RESULT_STUB[:-1],
        CONTEXT_BUDGET_TOOL_RESULT_STUB.lower(),
        CONTEXT_BUDGET_TOOL_RESULT_STUB + " The tool said: synthetic text.",
        "The tool said: " + CONTEXT_BUDGET_TOOL_RESULT_STUB,
        CONTEXT_BUDGET_TOOL_RESULT_STUB * 2,
    ],
)
def test_any_other_plain_text_result_still_fails_capture(content: str) -> None:
    context = capture_provider_context(messages=_with_result(content), tools=_tools())

    assert context.disposition == "unavailable"
    assert context.reasons == ("malformed_json",)
    assert context.canonical is None


def test_the_stub_quoted_as_a_json_string_is_ordinary_json_and_is_retained_as_sent() -> None:
    # A JSON string literal was already valid tool-result JSON before the stub
    # was admitted. It is retained by the ordinary JSON route, quotes and all,
    # so the capture still shows exactly the bytes sent.
    quoted = json.dumps(CONTEXT_BUDGET_TOOL_RESULT_STUB)

    context = capture_provider_context(messages=_with_result(quoted), tools=_tools())

    assert context.disposition == "retained"
    assert context.canonical is not None
    assert json.loads(context.canonical)["messages"][_TOOL_RESULT_INDEX]["content"] == quoted


def test_the_stub_is_admitted_only_as_a_tool_result_never_as_tool_call_arguments() -> None:
    context = capture_provider_context(
        messages=_with_result("{}", arguments=CONTEXT_BUDGET_TOOL_RESULT_STUB),
        tools=_tools(),
    )

    assert context.disposition == "unavailable"
    assert context.reasons == ("malformed_json",)


def test_a_stubbed_result_of_an_unknown_tool_is_still_unsupported() -> None:
    # A result is paired to a call whose arguments were checked under the same
    # tool name first, so the unknown tool is refused before its result is read.
    messages = _stubbed_request()
    messages[2]["tool_calls"][0]["function"]["name"] = "not_a_composer_tool"

    context = capture_provider_context(messages=messages, tools=_tools())

    assert context.disposition == "unavailable"
    assert context.reasons == ("unsupported",)


@pytest.mark.asyncio
async def test_the_reply_stage_request_under_pressure_quotes_every_result_whole_and_is_retained() -> None:
    # The reply stage sends no tool-role message: it quotes each tool record
    # inside a user message, where no JSON is required. Was: under this
    # pressure the earlier result was quoted as the stub. No result of the
    # turn in progress is stubbed now; the request is retained as sent.
    base = await _base_request_bytes()
    first, second = "f" * (base * 6 // 10), "s" * (base * 6 // 10)
    generous, _ = _service(10_000_000)
    unshed, unshed_tools, _ = await _prepare_reply(generous, _two_round_turn(generous, first, second))
    service, _ = _service(message_bytes(unshed, unshed_tools).total_bytes * 10 // 9)

    selected, tools, record = await _prepare_reply(service, _two_round_turn(service, first, second))

    assert record.stubbed_tool_call_ids == ()
    assert all(message["role"] != "tool" for message in selected)
    assert CONTEXT_BUDGET_TOOL_RESULT_STUB not in selected[5]["content"] and first in selected[5]["content"]
    # The gateway detaches the prompt-guard labels before it captures and sends.
    wire_messages = detach_prompt_guard_messages(selected)[0]
    context = capture_provider_context(messages=wire_messages, tools=tools)
    assert context.disposition == "retained"
    assert context.canonical is not None
    assert json.loads(context.canonical)["messages"][5]["content"] == wire_messages[5]["content"]


def _wire(message: dict[str, Any]) -> int:
    return len(canonical_json(message).encode("utf-8"))


def _round(call_id: str, result: str) -> list[dict[str, Any]]:
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": call_id, "type": "function", "function": {"name": "get_pipeline_state", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": call_id, "content": json.dumps({"rows": result})},
    ]


_PREFIX: list[dict[str, Any]] = [
    {"role": "system", "content": "Read the input data and build the pipeline. " * 90},
    {"role": "user", "content": "Build a CSV pipeline."},
]
_ROUND_1, _ROUND_2, _ROUND_3, _ROUND_4 = (_round(f"call-{n}", str(n) * size) for n, size in ((1, 3000), (2, 3000), (3, 500), (4, 500)))
_STUBBED_1 = stubbed_tool_message(_ROUND_1[1])

_ACROSS_A_STUB: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]], frozenset[int], frozenset[str], list[dict[str, Any]]]] = {
    # The anchor's request held the ORIGINAL first result; this decision stubs it.
    "original_in_anchor_stub_sent_now": (
        [*_PREFIX, *_ROUND_1, *_ROUND_2],
        [*_PREFIX, *_ROUND_1, *_ROUND_2, *_ROUND_3],
        frozenset({3, 5}),
        frozenset(),
        [*_PREFIX, _ROUND_1[0], _ROUND_2[0]],
    ),
    # The anchor's request already held the STUB; the loop still carries the
    # original at a sheddable position and the stub is held for this call.
    "stub_in_anchor_stub_held_now": (
        [*_PREFIX, _ROUND_1[0], _STUBBED_1, *_ROUND_2, *_ROUND_3],
        [*_PREFIX, *_ROUND_1, *_ROUND_2, *_ROUND_3, *_ROUND_4],
        frozenset({3, 5, 7}),
        frozenset({"tool:call-1"}),
        [*_PREFIX, _ROUND_1[0], _ROUND_2[0], _ROUND_3[0]],
    ),
    # Worst case for credit: the stub itself sits at a protected position in
    # both requests. It is the same message in both, so it is shared exactly once.
    "stub_in_anchor_and_protected_now": (
        [*_PREFIX, _ROUND_1[0], _STUBBED_1, *_ROUND_2, *_ROUND_3],
        [*_PREFIX, _ROUND_1[0], _STUBBED_1, *_ROUND_2, *_ROUND_3, *_ROUND_4],
        frozenset({5, 7}),
        frozenset(),
        [*_PREFIX, _ROUND_1[0], _STUBBED_1, _ROUND_2[0], _ROUND_3[0]],
    ),
}


def _provider_count(messages: list[dict[str, Any]], bytes_per_token: int) -> int:
    """A stand-in provider tokenizer: additive per message, at a fixed density."""
    return sum(math.ceil(_wire(message) / bytes_per_token) for message in messages)


def _expected_a2_tokens(size: int, anchor_tokens: int, anchor_wire_bytes: int, shared_bytes: int) -> int:
    rate = 3.0
    if anchor_tokens <= anchor_wire_bytes <= 8 * anchor_tokens:
        rate = min(rate, anchor_wire_bytes / anchor_tokens)
    return min(math.ceil(size / rate), anchor_tokens + size - shared_bytes)


def _decide_across_a_stub(case: str, bytes_per_token: int) -> dict[str, int]:
    """Run the real capture, anchor and budget decision for one case; only the tokenizer is a stand-in."""
    anchor_request, candidate, mutable, held, shared_messages = _ACROSS_A_STUB[case]
    private_request = capture_provider_context(messages=anchor_request, tools=None)
    assert private_request.disposition == "retained"
    anchor_call = replace(
        _earlier_call(()), prompt_tokens=_provider_count(anchor_request, bytes_per_token), private_request=private_request
    )
    anchor = request_anchor(
        calls=[anchor_call], model=anchor_call.model_requested, dialect=None, messages=candidate, tools=None, mutable_positions=mutable
    )
    assert anchor.tokens == anchor_call.prompt_tokens
    segments = [
        SheddableSegment(
            SheddableKind.TOOL_RESULT,
            f"tool:{candidate[index]['tool_call_id']}",
            _wire(candidate[index]) - _wire(stubbed_tool_message(candidate[index])),
        )
        for index in sorted(mutable)
    ]
    request_bytes = request_wire_bytes(candidate, None)
    # Price the unshed request under A2 so the high mark forces a shed even when
    # its all-byte arm is cheaper than the anchored arm.
    unshed_estimate = _expected_a2_tokens(request_bytes, anchor.tokens, anchor.wire_bytes, anchor.shared_bytes)
    decision = decide_context_budget(
        policy=ContextBudgetPolicy(unshed_estimate, 10_000_000, 0.85, 0.6),
        request_bytes=request_bytes,
        segments=segments,
        previously_shed=frozenset(),
        held=held,
        anchor_tokens=anchor.tokens,
        anchor_shared_bytes=anchor.shared_bytes,
        anchor_wire_bytes=anchor.wire_bytes,
        anchor_authored_bytes=anchor.authored_bytes,
        anchor_status=anchor.reason,
    )
    shed_ids = {key.removeprefix("tool:") for key in decision.shed_keys}
    sent = [stubbed_tool_message(m) if m["role"] == "tool" and m["tool_call_id"] in shed_ids else m for m in candidate]
    assert decision.pricing.mode == "authored_prefix"
    return {
        "anchor_tokens": anchor.tokens,
        "anchor_wire_bytes": anchor.wire_bytes,
        "shared_bytes": anchor.shared_bytes,
        "expected_shared_bytes": sum(_wire(message) for message in shared_messages) + len("[]"),
        "stubs_sent": sum(m["role"] == "tool" and m["content"] == CONTEXT_BUDGET_TOOL_RESULT_STUB for m in sent),
        "bytes_after": decision.request_bytes_after,
        "sent_wire_bytes": request_wire_bytes(sent, None),
        "estimated_tokens_after": decision.estimated_tokens_after,
        "provider_count_of_sent": _provider_count(sent, bytes_per_token),
    }


@pytest.mark.parametrize("bytes_per_token", [1, 4])
@pytest.mark.parametrize("case", sorted(_ACROSS_A_STUB))
def test_the_anchored_estimate_is_never_below_the_provider_count_across_a_stub(case: str, bytes_per_token: int) -> None:
    """The smaller A2 pricing arm still covers the stand-in provider's count.

    A stubbed position is never shared with a request that held the original,
    and a sheddable position is never shared at all. Only the identical
    protected stub can earn shared-byte credit, once.
    """
    figures = _decide_across_a_stub(case, bytes_per_token)

    assert figures["stubs_sent"] >= 1
    # Shared bytes are exactly the unchanged protected messages plus the equal, empty tool list.
    assert figures["shared_bytes"] == figures["expected_shared_bytes"]
    assert 0 <= figures["shared_bytes"] <= figures["bytes_after"]
    assert figures["bytes_after"] == figures["sent_wire_bytes"]
    assert figures["estimated_tokens_after"] == _expected_a2_tokens(
        figures["sent_wire_bytes"], figures["anchor_tokens"], figures["anchor_wire_bytes"], figures["shared_bytes"]
    )
    assert figures["estimated_tokens_after"] >= figures["provider_count_of_sent"]


class _CountingProvider(_RecordingProvider):
    """Reports a plausible prompt count unique to each request: its wire bytes less one."""

    async def complete(self, **kwargs: Any) -> Any:
        response = await super().complete(**kwargs)
        response.usage.prompt_tokens = _wire_bytes(kwargs) - 1
        return response


@pytest.mark.asyncio
async def test_the_budget_anchor_advances_call_by_call_through_a_turn_over_the_high_mark() -> None:
    # Was: the turn stubbed mid-way and the anchor advanced to the call that carried a stub.
    # No result of the turn is stubbed now; the anchor still advances to each previous call
    # and the estimate still covers the count the provider reported.
    base = await _base_request_bytes()
    # A quarter of the base per result still crosses the high mark mid-turn and keeps the
    # last request clear of the private capture's text bound (bounded_json), which three
    # tenths missed by about 3 KB once the skill grew.
    big = "r" * (base // 4)
    tool_data = {f"call-{n}": {"rows": big + str(n)} for n in range(1, 6)}
    service, session_id = _service(_cap_for(base))
    provider = _CountingProvider(
        _tool_turn("call-1"),
        _tool_turn("call-2"),
        _tool_turn("call-3"),
        _tool_turn("call-4"),
        _tool_turn("call-5"),
        _make_llm_response(content="Pipeline built."),
    )

    result = await _compose(service, session_id, [], provider, tool_data=tool_data)

    calls = result.llm_calls
    assert len(calls) == len(provider.requests) == 6
    budgets = [call.context_budget for call in calls]
    assert all(budget is not None for budget in budgets)
    assert [budget.stubbed_tool_call_ids for budget in budgets if budget is not None] == [()] * 6
    # The turn does cross the high mark: under the old rule it stubbed mid-way.
    policy = service._context_budget_policy
    over = [
        index
        for index, budget in enumerate(budgets)
        if budget is not None and budget.estimated_tokens_before > policy.shed_above * policy.max_prompt_tokens
    ]
    assert over and 0 < over[0] < len(calls) - 1
    for request in provider.requests:
        assert all(m["content"] != CONTEXT_BUDGET_TOOL_RESULT_STUB for m in request["messages"] if m["role"] == "tool")
    # Each later decision anchors on the call immediately before it: the
    # anchor's size and count are that call's, never an older one's.
    wires = [_wire_bytes(request) for request in provider.requests]
    assert len(set(wires)) == len(wires)
    for index in range(1, len(calls)):
        budget = budgets[index]
        assert budget is not None
        assert budget.anchor_status == "anchored"
        assert budget.anchor_wire_bytes == wires[index - 1]
        assert budget.anchor_prompt_tokens == calls[index - 1].prompt_tokens == wires[index - 1] - 1
        # At this provider's worst density (one token per byte) the estimate
        # still covers the count the provider reported for the request sent.
        assert 0 <= budget.anchor_shared_bytes <= budget.decision_wire_bytes_after
        assert budget.estimated_tokens_after >= wires[index] - 1 == calls[index].prompt_tokens
    # Every request was retained.
    assert [call.private_request.disposition for call in calls if call.private_request is not None] == ["retained"] * 6
