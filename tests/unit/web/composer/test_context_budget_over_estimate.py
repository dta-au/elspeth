"""The token estimate decides when to leave history out, never whether a message is sent.

Pure calls to the one decision function both the compose loop and the planner use. Both pass
``on_over_estimate="send"``; a caller that passes nothing keeps the tokens refusal.
"""

from __future__ import annotations

import math

import pytest

from elspeth.web.composer.context_budget import (
    ContextBudgetPolicy,
    ContextBudgetRefusal,
    SheddableKind,
    SheddableSegment,
    decide_context_budget,
)

_POLICY = ContextBudgetPolicy(max_prompt_tokens=200_000, max_request_bytes=960 * 1024, shed_above=0.85, shed_to=0.60)
# The fixed part of a real compose request (skill, catalog, tools) and how much of it is shipped text.
_FLOOR_BYTES = 280_578
_FLOOR_AUTHORED_BYTES = 277_963
# A pipeline of about 200 KB as the state message carries it.
_GRAPH_BYTES = 205_000


def _history(*sizes: int) -> list[SheddableSegment]:
    kinds = (SheddableKind.ASSISTANT_PROSE, SheddableKind.USER_REQUEST)
    return [SheddableSegment(kind=kinds[index % 2], key=f"history:{index}", saving_bytes=size) for index, size in enumerate(sizes)]


def test_protected_content_over_the_estimate_is_returned_and_marked() -> None:
    """No segments at all: today's estimate is over the setting, the bytes are far under the maximum."""
    request_bytes = _FLOOR_BYTES + 107_000

    decision = decide_context_budget(
        policy=_POLICY,
        request_bytes=request_bytes,
        segments=[],
        previously_shed=frozenset(),
        authored_bytes=_FLOOR_AUTHORED_BYTES,
        on_over_estimate="send",
    )

    assert decision.shed_keys == ()
    assert decision.request_bytes_before == decision.request_bytes_after == request_bytes
    assert decision.over_estimate in (True, False)


def test_protected_content_over_any_estimate_sheds_every_segment_and_is_marked_over() -> None:
    """Over the setting at one token per three bytes as well: everything that can go is gone, and it is sent."""
    request_bytes = 800_000
    segments = _history(4_000, 3_000, 2_000)

    decision = decide_context_budget(
        policy=_POLICY,
        request_bytes=request_bytes,
        segments=segments,
        previously_shed=frozenset(),
        authored_bytes=_FLOOR_AUTHORED_BYTES,
        on_over_estimate="send",
    )

    assert set(decision.shed_keys) == {segment.key for segment in segments}
    assert decision.request_bytes_after == request_bytes - 9_000
    assert decision.estimated_tokens_after > _POLICY.max_prompt_tokens
    assert decision.over_estimate is True


def test_a_request_within_the_estimate_is_not_marked_over() -> None:
    decision = decide_context_budget(
        policy=_POLICY,
        request_bytes=_FLOOR_BYTES + 10_000,
        segments=_history(1_000),
        previously_shed=frozenset(),
        authored_bytes=_FLOOR_AUTHORED_BYTES,
        on_over_estimate="send",
    )

    assert decision.shed_keys == ()
    assert decision.over_estimate is False


def test_control_the_default_caller_is_still_refused_over_the_estimate() -> None:
    """A caller that passes no keyword keeps the tokens refusal (no production caller does since the planner passes "send")."""
    with pytest.raises(ContextBudgetRefusal) as exc_info:
        decide_context_budget(
            policy=_POLICY, request_bytes=800_000, segments=_history(4_000), previously_shed=frozenset(), authored_bytes=0
        )

    assert exc_info.value.ceiling == "tokens"


def test_control_protected_content_over_the_byte_maximum_is_still_refused() -> None:
    """Generous tokens, so the byte comparison is the only one that can refuse."""
    policy = ContextBudgetPolicy(max_prompt_tokens=10_000_000, max_request_bytes=960 * 1024, shed_above=0.85, shed_to=0.60)
    with pytest.raises(ContextBudgetRefusal) as exc_info:
        decide_context_budget(
            policy=policy,
            request_bytes=policy.max_request_bytes + 1 + 5_000,
            segments=_history(5_000),
            previously_shed=frozenset(),
            authored_bytes=_FLOOR_AUTHORED_BYTES,
        )

    assert exc_info.value.ceiling == "bytes"


def test_protected_content_over_both_the_estimate_and_the_byte_maximum_is_refused_for_bytes() -> None:
    """Only the byte maximum refuses, so that is what the refusal names."""
    with pytest.raises(ContextBudgetRefusal) as exc_info:
        decide_context_budget(
            policy=_POLICY,
            request_bytes=_POLICY.max_request_bytes + 1 + 5_000,
            segments=_history(5_000),
            previously_shed=frozenset(),
            authored_bytes=_FLOOR_AUTHORED_BYTES,
            on_over_estimate="send",
        )

    assert exc_info.value.ceiling == "bytes"


def test_control_a_request_exactly_at_the_byte_maximum_is_not_refused_for_bytes() -> None:
    """At the maximum, not over it. Generous tokens so only the byte comparison is under test."""
    policy = ContextBudgetPolicy(max_prompt_tokens=10_000_000, max_request_bytes=960 * 1024, shed_above=0.85, shed_to=0.60)

    decision = decide_context_budget(
        policy=policy, request_bytes=policy.max_request_bytes, segments=[], previously_shed=frozenset(), authored_bytes=0
    )

    assert decision.request_bytes_after == policy.max_request_bytes


# Wire bytes and the provider's own prompt-token count for recorded requests (sizes only).
_RECORDED = (
    (238_247, 53_461),
    (240_507, 54_014),
    (284_441, 64_505),
)


@pytest.mark.parametrize(("wire_bytes", "provider_tokens"), _RECORDED)
def test_control_the_shipped_rate_never_prices_a_recorded_request_below_the_providers_count(wire_bytes: int, provider_tokens: int) -> None:
    """Every byte at the shipped rate: the estimate stays at or above what the provider counted."""
    decision = decide_context_budget(
        policy=_POLICY, request_bytes=wire_bytes, segments=[], previously_shed=frozenset(), authored_bytes=wire_bytes
    )

    assert decision.estimated_tokens_after == math.ceil(wire_bytes / 3.0)
    assert decision.estimated_tokens_after >= provider_tokens
