"""Message-shape helpers for applying a context-budget decision at a call site.

:mod:`elspeth.web.composer.context_budget` decides what to shed and knows
nothing about messages. These helpers measure a request exactly as the call
record measures it, build the stubbed form of a tool result, locate the most
recent tool round trip, and name the largest component for a refusal. They
write no content derived from user or model text: the only server-authored
string is the constant stub.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMRequestByteSplit
from elspeth.contracts.prompt_guard import (
    PROMPT_GUARD_CONTENT_PARTS_KEY,
    PROMPT_GUARD_TRUST_KEY,
    detach_prompt_guard_messages,
)
from elspeth.web.composer.context_budget import CONTEXT_BUDGET_TOOL_RESULT_STUB, SheddableKind, SheddableSegment
from elspeth.web.composer.context_budget_history import ContextBudgetHistory
from elspeth.web.composer.llm_response_parsing import _hash_and_measure_request


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
    return frozenset(shed)
