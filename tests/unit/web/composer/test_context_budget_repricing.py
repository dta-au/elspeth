"""Repricing: every byte of a request is priced at the shipped rate, or at the anchor's measured density if denser.

A request the provider counts well under the setting keeps its history. A
first call whose content is denser than the shipped rate is under-counted:
nothing is refused for it, the provider judges, and the next call is priced
at what the provider measured.
"""

from __future__ import annotations

import math
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from litellm.exceptions import ContextWindowExceededError

from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.context_budget import (
    ContextBudgetPolicy,
    ContextBudgetRefusal,
    SheddableKind,
    SheddableSegment,
    decide_context_budget,
)
from elspeth.web.composer.protocol import ComposerConvergenceError
from tests.unit.web.composer._helpers import _clean_advisor_checkpoint, _composer_service_with_session
from tests.unit.web.composer.test_compose_loop_context_budget import _RecordingProvider
from tests.unit.web.composer.test_compose_loop_llm_audit import (
    _empty_state,
    _make_llm_response,
    _make_settings,
    _mock_catalog,
    _passing_preflight,
)

_POLICY = ContextBudgetPolicy(max_prompt_tokens=200_000, max_request_bytes=960 * 1024, shed_above=0.85, shed_to=0.60)
_FLOOR_BYTES = 280_578
_FLOOR_AUTHORED_BYTES = 277_963
_GRAPH_BYTES = 205_000


def _history(*sizes: int) -> list[SheddableSegment]:
    kinds = (SheddableKind.ASSISTANT_PROSE, SheddableKind.USER_REQUEST)
    return [SheddableSegment(kind=kinds[index % 2], key=f"history:{index}", saving_bytes=size) for index, size in enumerate(sizes)]


def test_the_first_call_with_a_200_kb_pipeline_keeps_its_history() -> None:
    """Skill, catalog, tools and a 200 KB pipeline, with a little conversation: nothing needs to go.

    The provider counts such a request at well under the setting; an estimate
    that prices the pipeline at one token per byte sheds all of the history.
    """
    segments = _history(6_000, 5_000, 4_000)
    request_bytes = _FLOOR_BYTES + _GRAPH_BYTES + 15_000

    decision = decide_context_budget(
        policy=_POLICY,
        request_bytes=request_bytes,
        segments=segments,
        previously_shed=frozenset(),
        authored_bytes=_FLOOR_AUTHORED_BYTES,
    )

    assert decision.shed_keys == ()
    assert decision.estimated_tokens_after <= _POLICY.shed_above * _POLICY.max_prompt_tokens
    assert decision.over_estimate is False


def test_every_byte_pays_the_shipped_rate_whatever_is_labelled_authored() -> None:
    labelled = decide_context_budget(
        policy=_POLICY, request_bytes=300_001, segments=[], previously_shed=frozenset(), authored_bytes=300_001
    )
    unlabelled = decide_context_budget(policy=_POLICY, request_bytes=300_001, segments=[], previously_shed=frozenset(), authored_bytes=0)

    assert labelled.estimated_tokens_after == unlabelled.estimated_tokens_after == math.ceil(300_001 / 3.0)
    assert labelled.bytes_per_token_used == unlabelled.bytes_per_token_used == 3.0
    assert unlabelled.pricing.mode == "authored_prefix"
    assert unlabelled.pricing.authored_refuted is False


@pytest.mark.parametrize("on_over_estimate", ["refuse", "send"])
def test_a_first_call_denser_than_the_shipped_rate_is_under_counted_and_nothing_is_refused_or_shed(on_over_estimate: Any) -> None:
    """450 KB that the provider would count at 2.0 bytes per token (225,000) is estimated at 150,000."""
    decision = decide_context_budget(
        policy=_POLICY,
        request_bytes=450_000,
        segments=_history(6_000, 5_000),
        previously_shed=frozenset(),
        on_over_estimate=on_over_estimate,
    )

    assert decision.estimated_tokens_after == 150_000
    assert decision.shed_keys == ()
    assert decision.over_estimate is False


def _after_a_dense_call(**overrides: Any) -> dict[str, Any]:
    """The next call of that conversation: the provider counted the 450 KB request at 225,000 tokens."""
    return {
        "policy": _POLICY,
        "request_bytes": 460_000,
        "segments": _history(6_000, 5_000),
        "previously_shed": frozenset(),
        "anchor_tokens": 225_000,
        "anchor_wire_bytes": 450_000,
        "anchor_shared_bytes": 440_000,
        "anchor_status": "anchored",
        **overrides,
    }


def test_the_next_call_is_priced_at_the_density_the_provider_measured() -> None:
    decision = decide_context_budget(**_after_a_dense_call(on_over_estimate="send"))

    # 2.0 bytes per token replaces 3.0; nothing is priced below what the provider's own ratio implies.
    assert decision.bytes_per_token_used == 2.0
    assert decision.pricing.authored_refuted is True
    assert decision.estimated_tokens_after == math.ceil(decision.request_bytes_after / 2.0)
    # Over the setting at the measured density: everything that can go has gone, and it is sent as over.
    assert set(decision.shed_keys) == {"history:0", "history:1"}
    assert decision.over_estimate is True


def test_control_the_default_caller_refuses_that_next_call_on_the_measured_density() -> None:
    with pytest.raises(ContextBudgetRefusal) as exc_info:
        decide_context_budget(**_after_a_dense_call())

    assert exc_info.value.ceiling == "tokens"
    assert exc_info.value.estimated_tokens == math.ceil((460_000 - 11_000) / 2.0)


@pytest.mark.parametrize(
    ("anchor_tokens", "anchor_wire_bytes"),
    [
        (100_000, 450_000),  # sparser than the shipped rate: never loosens it
        (50_000, 450_000),  # more than eight bytes per token: not a plausible measurement
        (500_000, 450_000),  # more tokens than bytes: not a plausible measurement
    ],
)
def test_control_only_a_plausible_denser_anchor_changes_the_rate(anchor_tokens: int, anchor_wire_bytes: int) -> None:
    decision = decide_context_budget(
        **_after_a_dense_call(
            anchor_tokens=anchor_tokens, anchor_wire_bytes=anchor_wire_bytes, anchor_shared_bytes=0, on_over_estimate="send"
        )
    )

    assert decision.pricing.authored_refuted is False
    assert decision.bytes_per_token_used == 3.0
    assert decision.estimated_tokens_after == math.ceil(decision.request_bytes_after / 3.0)


def test_shed_history_comes_back_at_the_low_mark_of_the_same_price() -> None:
    """300 KB is 100,000 tokens at the shipped rate, under the low mark of 120,000: earlier shedding is released."""
    decision = decide_context_budget(
        policy=_POLICY, request_bytes=300_000, segments=_history(6_000, 5_000), previously_shed=frozenset({"history:0", "history:1"})
    )

    assert decision.shed_keys == ()


def test_control_shed_history_stays_shed_above_the_low_mark() -> None:
    decision = decide_context_budget(
        policy=_POLICY, request_bytes=400_000, segments=_history(6_000, 5_000), previously_shed=frozenset({"history:0", "history:1"})
    )

    assert set(decision.shed_keys) == {"history:0", "history:1"}


@pytest.mark.parametrize(
    ("anchor_tokens", "anchor_wire_bytes", "anchor_shared_bytes"),
    [
        (None, 0, 0),
        (60_000, 300_000, 0),  # sparse
        (100_000, 300_000, 0),  # at the policy rate
        (290_000, 300_000, 0),  # very dense
        # A count that prices this request at 60,000 by the anchored arm, well under the low mark.
        # A restore rule that read the anchor would release the history here.
        (40_000, 380_000, 380_000),
        (None, 0, 0),
    ],
)
def test_the_restore_decision_never_uses_the_anchor(anchor_tokens: int | None, anchor_wire_bytes: int, anchor_shared_bytes: int) -> None:
    """At the shipped rate, 400 KB is 133,334 tokens: above the low mark of 120,000, so what was shed stays shed.

    The high mark (850,000) is out of reach, so nothing here is shed for being over: only the
    restore rule decides. No anchor, a sparse one, one at the policy rate, a very dense one
    and one that would price the request under the low mark all give the same answer: an
    anchor arriving or vanishing releases nothing.
    """
    decision = decide_context_budget(
        policy=ContextBudgetPolicy(max_prompt_tokens=1_000_000, max_request_bytes=960 * 1024, shed_above=0.85, shed_to=0.12),
        request_bytes=400_000,
        segments=_history(6_000, 5_000),
        previously_shed=frozenset({"history:0", "history:1"}),
        anchor_tokens=anchor_tokens,
        anchor_wire_bytes=anchor_wire_bytes,
        anchor_shared_bytes=anchor_shared_bytes,
        anchor_status="anchored" if anchor_tokens is not None else "no_previous_call",
    )

    assert set(decision.shed_keys) == {"history:0", "history:1"}


@pytest.mark.parametrize(("anchor_tokens", "anchor_wire_bytes"), [(None, 0), (60_000, 300_000), (290_000, 300_000)])
def test_the_restore_decision_at_the_low_mark_is_the_same_with_and_without_an_anchor(
    anchor_tokens: int | None, anchor_wire_bytes: int
) -> None:
    """300 KB is 100,000 tokens at the policy rate, under the low mark: released whatever the anchor says."""
    decision = decide_context_budget(
        policy=ContextBudgetPolicy(max_prompt_tokens=10_000_000, max_request_bytes=960 * 1024, shed_above=0.85, shed_to=0.012),
        request_bytes=300_000,
        segments=_history(6_000, 5_000),
        previously_shed=frozenset({"history:0", "history:1"}),
        anchor_tokens=anchor_tokens,
        anchor_wire_bytes=anchor_wire_bytes,
        anchor_status="anchored" if anchor_tokens is not None else "no_previous_call",
    )

    assert decision.shed_keys == ()


# --- The compose loop ---------------------------------------------------------------------------------


@pytest.fixture
def _advisor_end_gate_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """The END advisor gate is not under test; it returns CLEAN without a provider call."""
    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _clean_advisor_checkpoint, raising=True)


async def _compose(provider: Any) -> Any:
    service, session_id = _composer_service_with_session(
        catalog=_mock_catalog(), settings=_make_settings(composer_context_max_prompt_tokens=200_000)
    )
    with (
        patch("litellm.acompletion", new_callable=AsyncMock, side_effect=provider),
        patch.object(service._preflight, "cached_runtime_preflight", new_callable=AsyncMock, return_value=_passing_preflight()),
    ):
        return await service.compose("Build a CSV pipeline", [], _empty_state(), session_id=session_id)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_advisor_end_gate_clean")
async def test_the_call_record_carries_the_rate_the_estimate_applied() -> None:
    provider = _RecordingProvider(_make_llm_response(content="Done."))

    result = await _compose(provider.complete)

    (call,) = result.llm_calls
    record = call.context_budget
    assert record is not None
    assert record.bytes_per_token == "3.0"
    assert record.pricing_mode == "authored_prefix"
    assert record.estimated_tokens_after == math.ceil(record.decision_wire_bytes_after / 3.0)
    assert record.over_estimate is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("_advisor_end_gate_clean")
async def test_a_request_within_the_estimate_that_the_gateway_rejects_as_too_long_is_bubbled_back_as_the_size_refusal() -> None:
    """The under-count case end to end: nothing refused locally, one request sent, the gateway's refusal reaches the user."""
    requests: list[dict[str, Any]] = []

    async def reject(**kwargs: Any) -> Any:
        requests.append(kwargs)
        raise ContextWindowExceededError(
            message="AnthropicError - prompt is too long: 251000 tokens > 200000 maximum", model="test-model", llm_provider="anthropic"
        )

    with pytest.raises(ComposerConvergenceError) as exc_info:
        await _compose(reject)

    assert len(requests) == 1
    error = exc_info.value
    assert error.reason == "context_budget_exceeded"
    assert error.evidence["ceiling"] == "provider"
    assert error.evidence["estimated_tokens"] <= 200_000
    (call,) = error.llm_calls
    assert call.context_budget is not None and call.context_budget.over_estimate is False
