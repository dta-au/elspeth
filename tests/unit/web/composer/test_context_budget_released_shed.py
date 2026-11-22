"""A segment the release rule restored can be shed again in the same call.

Release is priced at the policy rate (3.0 bytes per token); whether the request is over the high
mark is priced at the anchor's measured density when that is denser. Below about 2.1 bytes per
token a request can be released and still over the high mark. The released segment must then be
eligible to shed like any other; before, it was neither kept shed nor sheddable, so a request
that fits once it is shed was refused ("refuse") or raised an invariant error ("send").
"""

from __future__ import annotations

from typing import Literal

import pytest

from elspeth.web.composer.context_budget import (
    ContextBudgetDecision,
    ContextBudgetPolicy,
    SheddableKind,
    SheddableSegment,
    decide_context_budget,
)

# Shipped rate (3.0): low mark 600 tokens, high mark 850.
_POLICY = ContextBudgetPolicy(max_prompt_tokens=1000, max_request_bytes=10**6, shed_above=0.85, shed_to=0.6)
_SEGMENT = SheddableSegment(kind=SheddableKind.TOOL_RESULT, key="tool:a", saving_bytes=800)
# 1,700 B is 567 tokens at 3.0 (at or below the low mark: released) but 1,700 tokens at the
# anchor's one byte per token (over the high mark and over the 1,000-token estimate).
_REQUEST_BYTES = 1700


def _decide(
    *, request_bytes: int, previously_shed: frozenset[str], mode: Literal["refuse", "send"], anchor_bytes: int
) -> ContextBudgetDecision:
    # An anchor of one token per byte: the measured density replaces the 3.0 policy rate.
    return decide_context_budget(
        policy=_POLICY,
        request_bytes=request_bytes,
        segments=[_SEGMENT],
        previously_shed=previously_shed,
        on_over_estimate=mode,
        anchor_tokens=anchor_bytes,
        anchor_wire_bytes=anchor_bytes,
        anchor_shared_bytes=anchor_bytes,
    )


def test_released_segment_is_shed_and_not_refused_under_refuse() -> None:
    """The planner's mode: at 709e171ca this raised the tokens refusal (estimated 1,700)."""
    decision = _decide(request_bytes=_REQUEST_BYTES, previously_shed=frozenset({"tool:a"}), mode="refuse", anchor_bytes=900)

    assert decision.shed_keys == ("tool:a",)
    assert decision.request_bytes_after == 900
    assert decision.estimated_tokens_after == 900
    assert decision.over_estimate is False


def test_released_segment_is_shed_and_sent_within_the_estimate_under_send() -> None:
    """The compose loop's mode: at 709e171ca this raised InvariantError (a segment still unshed)."""
    decision = _decide(request_bytes=_REQUEST_BYTES, previously_shed=frozenset({"tool:a"}), mode="send", anchor_bytes=900)

    assert decision.shed_keys == ("tool:a",)
    assert decision.request_bytes_after == 900
    assert decision.estimated_tokens_after == 900
    assert decision.over_estimate is False


@pytest.mark.parametrize("mode", ["send", "refuse"])
def test_released_segment_is_shed_to_the_high_mark_at_a_plausible_dense_anchor(mode: Literal["refuse", "send"]) -> None:
    """2.0 B/token, inside the window (below 3.0 x 0.60 / 0.85 = 2.12): released at 3.0, over the high mark at 2.0.

    1,750 B is 584 tokens at 3.0 (released) and 875 at 2.0 (over 850). At 709e171ca nothing was shed and the
    request went at 875 tokens, above the high mark; neither refusal nor error, so only the shed keys show it.
    """
    segment = SheddableSegment(kind=SheddableKind.TOOL_RESULT, key="tool:a", saving_bytes=400)
    decision = decide_context_budget(
        policy=_POLICY,
        request_bytes=1750,
        segments=[segment],
        previously_shed=frozenset({"tool:a"}),
        on_over_estimate=mode,
        anchor_tokens=800,
        anchor_wire_bytes=1600,
    )

    assert decision.bytes_per_token_used == 2.0
    assert decision.estimated_tokens_before == 875
    assert decision.shed_keys == ("tool:a",)
    assert decision.request_bytes_after == 1350
    assert decision.estimated_tokens_after == 675
    assert decision.over_estimate is False


@pytest.mark.parametrize("mode", ["send", "refuse"])
def test_released_segment_is_decided_as_if_never_shed(mode: Literal["refuse", "send"]) -> None:
    """The shed history does not change the outcome once the release rule has fired."""
    released = _decide(request_bytes=_REQUEST_BYTES, previously_shed=frozenset({"tool:a"}), mode=mode, anchor_bytes=900)
    fresh = _decide(request_bytes=_REQUEST_BYTES, previously_shed=frozenset(), mode=mode, anchor_bytes=900)

    assert released == fresh


@pytest.mark.parametrize("mode", ["send", "refuse"])
def test_released_segment_stays_restored_when_the_request_fits(mode: Literal["refuse", "send"]) -> None:
    """The release rule still restores: at the same anchor density, a request at the high mark keeps it."""
    request_bytes = 850  # 850 tokens at one byte per token: at the high mark, not over it.
    decision = _decide(request_bytes=request_bytes, previously_shed=frozenset({"tool:a"}), mode=mode, anchor_bytes=50)

    assert "tool:a" not in decision.shed_keys
    assert decision.shed_keys == ()
    assert decision.request_bytes_after == request_bytes
    assert decision.estimated_tokens_after == 850
    assert decision.over_estimate is False
