"""Message-shape helpers for applying a context-budget decision at a call site.

:mod:`elspeth.web.composer.context_budget` decides what to shed and knows
nothing about messages. These helpers measure a request exactly as the call
record measures it, build the stubbed form of a tool result, label stubbable
tool results, compose the previously shed keys, locate the most recent tool
round trip, name the largest component for a refusal, and say whether a call
is the first of its turn to shed conversation history. They
write no content derived from user or model text: the only server-authored
string is the constant stub.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

from elspeth.contracts.composer_llm_audit import ComposerContextBudgetRecord, ComposerLLMCall, ComposerLLMRequestByteSplit
from elspeth.contracts.prompt_guard import (
    PROMPT_GUARD_CONTENT_PARTS_KEY,
    PROMPT_GUARD_TRUST_KEY,
    detach_prompt_guard_messages,
)
from elspeth.web.composer.context_budget import CONTEXT_BUDGET_TOOL_RESULT_STUB, SheddableKind, SheddableSegment
from elspeth.web.composer.context_budget_history import ContextBudgetHistory
from elspeth.web.composer.llm_response_parsing import _hash_and_measure_request

# Worst-case bytes a re-render after shedding history can add to a request
# that the segment savings do not count. It bounds: the compose loop's
# ``"conversation_omissions":{...}`` object with both counts; on the planner,
# the ``"prior_assistant_messages_omitted":N`` key and the growth of
# ``additional_prior_user_requests_omitted``'s digits, both as JSON-escaped
# inside the request string; and the one separator a saving over-counts when
# the last entry of a list is removed. Counts are sized as 20-digit integers.
# Each call site adds it to the request size it feeds the decision whenever
# history may be shed, so an admitted request still fits once re-rendered.
CONTEXT_BUDGET_OMISSION_ALLOWANCE_BYTES: Final[int] = 256

# Worst-case bytes rebuilding the escape hatch's transcript without some events
# can add to a request that the event savings do not count: the
# ``"oldest_events_omitted_for_context_budget"`` gap with its quotes and
# separator, as JSON-escaped inside the transcript message's content (46), and
# the one byte an event's saving over-counts when the transcript's only event
# is removed. Those total 47; the rest is margin so a longer gap spelling or
# one more separator-level edge cannot silently turn the bound into an
# over-count. The planner adds it whenever transcript events may be shed.
CONTEXT_BUDGET_TRANSCRIPT_GAP_ALLOWANCE_BYTES: Final[int] = 64


def message_bytes(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> ComposerLLMRequestByteSplit:
    """Measure a request the way its call record will, after detaching prompt-guard keys."""
    return _hash_and_measure_request(detach_prompt_guard_messages(messages)[0], tools)[2]


def stubbed_tool_message(message: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a tool result whose content is the constant stub.

    ``role`` and ``tool_call_id`` are unchanged, so the provider's call/result
    pairing holds. The original content's prompt-guard provenance no longer
    describes the content and is dropped; the stub is screened as ordinary
    untrusted tool content.
    """
    if message["role"] != "tool":
        raise ValueError("only a tool result message can be stubbed")
    stubbed = {key: value for key, value in message.items() if key not in (PROMPT_GUARD_CONTENT_PARTS_KEY, PROMPT_GUARD_TRUST_KEY)}
    stubbed["content"] = CONTEXT_BUDGET_TOOL_RESULT_STUB
    return stubbed


def latest_round_trip_start(messages: list[dict[str, Any]]) -> int | None:
    """Return the index of the last assistant message that carries tool calls."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message["role"] == "assistant" and "tool_calls" in message and message["tool_calls"]:
            return index
    return None


def largest_component(named_bytes: Mapping[str, int]) -> str:
    """Return the name of the largest component; the first named wins a tie."""
    if not named_bytes:
        raise ValueError("largest_component needs at least one named component")
    largest_name = ""
    largest_size = -1
    for name, size in named_bytes.items():
        if size > largest_size:
            largest_name, largest_size = name, size
    return largest_name


def tool_result_segments(
    messages: list[dict[str, Any]], split: ComposerLLMRequestByteSplit, *, start: int, stop: int
) -> tuple[list[SheddableSegment], dict[str, list[int]]]:
    """Label the tool results in ``messages[start:stop]`` that stubbing would shrink.

    ``split`` is ``message_bytes(messages, ...)``. Each segment is keyed
    ``tool:<tool_call_id>`` in request order. A provider may reuse a tool-call
    id across rounds; one key then covers every result in the range that
    carries it, and the returned positions list each of them.
    """
    positions: dict[str, list[int]] = {}
    savings: dict[str, int] = {}
    for index in range(start, stop):
        message = messages[index]
        if message["role"] != "tool":
            continue
        saving = split.message_bytes[index] - message_bytes([stubbed_tool_message(message)], None).total_bytes
        if saving <= 0:
            continue
        key = f"tool:{message['tool_call_id']}"
        positions.setdefault(key, []).append(index)
        savings[key] = savings.get(key, 0) + saving
    segments = [SheddableSegment(kind=SheddableKind.TOOL_RESULT, key=key, saving_bytes=saving) for key, saving in savings.items()]
    return segments, positions


def previously_shed_keys(history: ContextBudgetHistory, calls: Iterable[ComposerLLMCall]) -> frozenset[str]:
    """The session's previously shed history united with every key recorded on ``calls``."""
    shed = set(history.previously_shed_history)
    for call in calls:
        if call.context_budget is not None:
            shed.update(f"history:{index}" for index in call.context_budget.shed_history_indices)
            shed.update(f"tool:{tool_call_id}" for tool_call_id in call.context_budget.stubbed_tool_call_ids)
            shed.update(f"hatch:{index}" for index in call.context_budget.shed_hatch_event_indices)
    return frozenset(shed)


def first_history_shed_of_turn(record: ComposerContextBudgetRecord, earlier_calls: Iterable[ComposerLLMCall]) -> bool:
    """Whether this call's decision sheds conversation history and no earlier call of the turn did.

    ``record`` is this call's decision; ``earlier_calls`` are the calls the
    turn recorded before it. Stubbing tool results or leaving out escape-hatch
    transcript events sheds no conversation history. A call site announces the
    shed to the user exactly when this holds, so a turn announces it once.
    """
    if not record.shed_history_indices:
        return False
    return not any(call.context_budget is not None and call.context_budget.shed_history_indices for call in earlier_calls)
