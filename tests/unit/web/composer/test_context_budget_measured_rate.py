"""The byte rate is a measured same-model density, less a margin, for admission and release alike.

A session's newest successful budgeted call of the same model and tool dialect gives the density
(the wire bytes it sent over the provider's prompt count). The first call of a turn has no live
anchor, so the density must come from the stored session as well as from this exchange's calls:
otherwise each turn's first call would be priced at the 3.0 fallback and the next at the measured
density, and what is shed would flip at every turn boundary.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.context_budget import (
    CONTEXT_BUDGET_MEASURED_MARGIN,
    ContextBudgetDecision,
    ContextBudgetPolicy,
    SheddableKind,
    SheddableSegment,
    decide_context_budget,
)
from elspeth.web.composer.context_budget_history import ContextBudgetHistory, MeasuredDensity, MeasuredRate
from tests.unit.web.composer.test_context_budget_history import _COMPOSER, _audit_row, _call

# A recorded 0.8.17 session (one model, sizes and audit budget fields only). Its first shed:
# a 518,623 B request whose live anchor was 108,710 tokens for 481,960 B (4.43 B/token), with
# 307,443 B shared; leaving out the earlier turns saved at least 8,625 B. The provider later
# counted that request at 115,258 tokens, 58% of the 200,000 setting.
_RECORDED_POLICY = ContextBudgetPolicy(max_prompt_tokens=200_000, max_request_bytes=960 * 1024, shed_above=0.85, shed_to=0.60)
_RECORDED = {
    "request_bytes": 518_623,
    "anchor_tokens": 108_710,
    "anchor_wire_bytes": 481_960,
    "anchor_shared_bytes": 307_443,
}
_RECORDED_DENSITY = 481_960 / 108_710
_HISTORY = SheddableSegment(kind=SheddableKind.ASSISTANT_PROSE, key="history:0", saving_bytes=8_625)


def _decide(
    *, measured: float | None, previously_shed: frozenset[str] = frozenset(), anchored: bool = True, **sizes: int
) -> ContextBudgetDecision:
    recorded = {**_RECORDED, **sizes}
    anchor: dict[str, Any] = (
        {
            "anchor_tokens": recorded["anchor_tokens"],
            "anchor_wire_bytes": recorded["anchor_wire_bytes"],
            "anchor_shared_bytes": recorded["anchor_shared_bytes"],
        }
        if anchored
        else {}
    )
    return decide_context_budget(
        policy=_RECORDED_POLICY,
        request_bytes=recorded["request_bytes"],
        segments=[_HISTORY],
        previously_shed=previously_shed,
        on_over_estimate="send",
        measured_bytes_per_token=measured,
        **anchor,
    )


def test_the_recorded_first_shed_keeps_the_earlier_turns_at_the_measured_rate() -> None:
    # Control: at the 3.0 fallback the recorded request is over the high mark (172,875 > 170,000) and sheds.
    fallback = _decide(measured=None)
    assert fallback.estimated_tokens_before == math.ceil(518_623 / 3.0) == 172_875
    assert fallback.shed_keys == ("history:0",)

    measured = _decide(measured=_RECORDED_DENSITY)

    assert measured.shed_keys == ()
    assert measured.estimated_tokens_before == math.ceil(518_623 / (_RECORDED_DENSITY * CONTEXT_BUDGET_MEASURED_MARGIN))
    # Still above what the provider counted for that request: the margin errs high.
    assert measured.estimated_tokens_before > 115_258
    assert measured.pricing.authored_refuted is True
    assert measured.bytes_per_token_used == _RECORDED_DENSITY * CONTEXT_BUDGET_MEASURED_MARGIN


def test_release_at_a_turn_boundary_is_the_same_as_on_the_call_after_it() -> None:
    # 450,000 B: at 0.9 x 4.43 B/token it is 112,800 tokens, under the low mark (120,000); at 3.0 it
    # is 150,000, over it. The turn's first call has no live anchor; the next call has one.
    shed = frozenset({"history:0"})
    first_of_turn = _decide(measured=_RECORDED_DENSITY, previously_shed=shed, anchored=False, request_bytes=450_000)
    next_call = _decide(measured=_RECORDED_DENSITY, previously_shed=shed, anchored=True, request_bytes=450_000)

    assert first_of_turn.shed_keys == next_call.shed_keys == ()

    # Control: a first call priced without the session's density (a live anchor only) keeps the
    # earlier turns shed, so the shed set would flip between the two calls of every turn.
    unmeasured = _decide(measured=None, previously_shed=shed, anchored=False, request_bytes=450_000)
    assert unmeasured.shed_keys == ("history:0",)


def test_retained_content_denser_than_the_measured_mix_is_not_under_counted() -> None:
    # The provider reports one count for the whole request. If the content that remains is 10%
    # denser than the mix that was measured, the margin still prices it at or above its tokens.
    size = 500_000
    denser_tokens = math.ceil(size / (_RECORDED_DENSITY / 1.10))
    decision = decide_context_budget(
        policy=_RECORDED_POLICY,
        request_bytes=size,
        segments=[],
        previously_shed=frozenset(),
        on_over_estimate="send",
        measured_bytes_per_token=_RECORDED_DENSITY,
    )
    assert decision.estimated_tokens_before >= denser_tokens
    # Control: at the plain measured density (no margin) the same content is under-counted.
    assert math.ceil(size / _RECORDED_DENSITY) < denser_tokens


@pytest.mark.parametrize("measured", [0.5, 8.5, 4], ids=["too-dense", "too-sparse", "not-a-float"])
def test_an_implausible_measured_density_is_refused(measured: Any) -> None:
    with pytest.raises(ValueError, match="plausible band"):
        _decide(measured=measured)


def test_without_a_measured_density_the_fallback_rate_is_unchanged() -> None:
    decision = decide_context_budget(
        policy=_RECORDED_POLICY, request_bytes=300_000, segments=[], previously_shed=frozenset(), on_over_estimate="send"
    )
    assert decision.estimated_tokens_before == 100_000
    assert decision.bytes_per_token_used == 3.0
    assert decision.pricing.authored_refuted is False


def _dialect_call(dialect: str | None, **fields: Any) -> dict[str, Any]:
    call = _call(**fields)
    call["tool_contract_dialect"] = dialect
    return call


def test_stored_history_keeps_the_newest_plausible_density_per_model_and_dialect() -> None:
    other_model = "some/other-model"
    rows = [
        _audit_row(1, _dialect_call(None, total_bytes=4_000, prompt_tokens=1_000)),
        _audit_row(2, _dialect_call(None, total_bytes=4_400, prompt_tokens=1_000)),
        # Newer, but failed, implausible (9 B/token) or without a stored size: none gives a density.
        _audit_row(3, _dialect_call(None, status="api_error", total_bytes=5_000, prompt_tokens=1_000)),
        _audit_row(4, _dialect_call(None, total_bytes=9_000, prompt_tokens=1_000)),
        _audit_row(5, _dialect_call(None, total_bytes=None, prompt_tokens=1_000)),
        _audit_row(6, _dialect_call(None, total_bytes=3_500, prompt_tokens=1_000, model=other_model)),
        _audit_row(7, _dialect_call(ToolContractDialect.NONE.value, total_bytes=2_500, prompt_tokens=1_000)),
    ]
    history = ContextBudgetHistory.from_records(rows)

    assert set(history.measured) == {
        MeasuredDensity(_COMPOSER, None, 4.4),
        MeasuredDensity(other_model, None, 3.5),
        MeasuredDensity(_COMPOSER, ToolContractDialect.NONE, 2.5),
    }
    assert history.measured_rate(calls=(), model=_COMPOSER, dialect=None) == MeasuredRate(4.4, "stored_call")
    assert history.measured_rate(calls=(), model=_COMPOSER, dialect=ToolContractDialect.NONE) == MeasuredRate(2.5, "stored_call")
    assert history.measured_rate(calls=(), model="never/seen", dialect=None) is None


def test_a_density_from_this_exchange_is_named_a_live_call_and_wins_over_the_stored_one() -> None:
    from tests.unit.contracts.test_composer_llm_audit import _budget_record, _make_call

    history = ContextBudgetHistory.from_records([_audit_row(1, _call(total_bytes=4_400, prompt_tokens=1_000))])
    live = _make_call(model_requested=_COMPOSER, prompt_tokens=1_000, context_budget=_budget_record(request_wire_bytes=3_000))

    assert history.measured_rate(calls=(live,), model=_COMPOSER, dialect=None) == MeasuredRate(3.0, "live_call")
    # Control: without the live call the stored density is the one used, and named so.
    assert history.measured_rate(calls=(), model=_COMPOSER, dialect=None) == MeasuredRate(4.4, "stored_call")


def test_a_record_without_a_dialect_key_is_read_as_dialect_free() -> None:
    history = ContextBudgetHistory.from_records([_audit_row(1, _call(total_bytes=4_000, prompt_tokens=1_000))])
    assert history.measured == (MeasuredDensity(_COMPOSER, None, 4.0),)


def test_a_stored_dialect_outside_the_vocabulary_is_corruption() -> None:
    with pytest.raises(AuditIntegrityError, match="tool_contract_dialect"):
        ContextBudgetHistory.from_records([_audit_row(1, _dialect_call("not-a-dialect"))])


# The compose path from stored history to the turn's first call is pinned in
# test_context_budget_turn_in_progress.py, with base symbols only.
