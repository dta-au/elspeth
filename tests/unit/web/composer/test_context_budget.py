"""Pure decision tests for the composer context budget."""

from __future__ import annotations

import dataclasses

import pytest

from elspeth.web.composer.context_budget import (
    CONTEXT_BUDGET_FALLBACK_BYTES_PER_TOKEN,
    CONTEXT_BUDGET_TOOL_RESULT_STUB,
    ContextBudgetDecision,
    ContextBudgetPolicy,
    ContextBudgetRefusal,
    SheddableKind,
    SheddableSegment,
    decide_context_budget,
)

TOOL_RESULT = SheddableKind.TOOL_RESULT
ASSISTANT_PROSE = SheddableKind.ASSISTANT_PROSE
HATCH_EVENT = SheddableKind.HATCH_EVENT
USER_REQUEST = SheddableKind.USER_REQUEST


def _policy(cap: int = 1000, max_bytes: int = 10**9, above: float = 0.85, to: float = 0.60) -> ContextBudgetPolicy:
    return ContextBudgetPolicy(max_prompt_tokens=cap, max_request_bytes=max_bytes, shed_above=above, shed_to=to)


def _seg(kind: SheddableKind, key: str, saving: int) -> SheddableSegment:
    return SheddableSegment(kind=kind, key=key, saving_bytes=saving)


def _segs(*segments: SheddableSegment) -> list[SheddableSegment]:
    return list(segments)


def test_constants_are_exact() -> None:
    assert CONTEXT_BUDGET_FALLBACK_BYTES_PER_TOKEN == 3.0
    assert CONTEXT_BUDGET_TOOL_RESULT_STUB == "[Result omitted by ELSPETH to fit the context budget. Call the tool again if you need it.]"
    assert [k.value for k in SheddableKind] == ["tool_result", "assistant_prose", "hatch_event", "user_request"]


def test_below_high_mark_sheds_nothing() -> None:
    d = decide_context_budget(policy=_policy(), request_bytes=850, segments=_segs(), bytes_per_token=1.0, previously_shed=frozenset())
    assert d.shed_keys == () and d.estimated_tokens_after == 850


def test_crossing_high_mark_sheds_in_kind_order_oldest_first_to_low_mark() -> None:
    segs = [
        _seg(USER_REQUEST, "history:0", 100),
        _seg(TOOL_RESULT, "tool:a", 100),
        _seg(TOOL_RESULT, "tool:b", 100),
        _seg(ASSISTANT_PROSE, "history:1", 100),
        _seg(HATCH_EVENT, "hatch:0", 100),
    ]
    d = decide_context_budget(policy=_policy(), request_bytes=900, segments=segs, bytes_per_token=1.0, previously_shed=frozenset())
    assert d.shed_keys == ("tool:a", "tool:b", "history:1")
    assert d.estimated_tokens_after == 600


def test_byte_ceiling_overflow_sheds_even_under_token_high_mark() -> None:
    d = decide_context_budget(
        policy=_policy(cap=10**9, max_bytes=500),
        request_bytes=560,
        segments=[_seg(TOOL_RESULT, "tool:a", 100)],
        bytes_per_token=1.0,
        previously_shed=frozenset(),
    )
    assert d.shed_keys == ("tool:a",) and d.request_bytes_after == 460


def test_previously_shed_stays_shed_between_marks() -> None:
    d = decide_context_budget(
        policy=_policy(),
        request_bytes=700,
        segments=[_seg(ASSISTANT_PROSE, "history:1", 50)],
        bytes_per_token=1.0,
        previously_shed=frozenset({"history:1"}),
    )
    assert d.shed_keys == ("history:1",)


def test_previously_shed_is_released_at_or_below_low_mark() -> None:
    d = decide_context_budget(
        policy=_policy(),
        request_bytes=600,
        segments=[_seg(ASSISTANT_PROSE, "history:1", 50)],
        bytes_per_token=1.0,
        previously_shed=frozenset({"history:1"}),
    )
    assert d.shed_keys == ()


def test_unknown_previously_shed_keys_are_ignored() -> None:
    d = decide_context_budget(
        policy=_policy(),
        request_bytes=700,
        segments=[],
        bytes_per_token=1.0,
        previously_shed=frozenset({"history:99", "tool:gone"}),
    )
    assert d.shed_keys == ()


def test_everything_shed_but_under_ceiling_is_allowed() -> None:
    d = decide_context_budget(
        policy=_policy(),
        request_bytes=990,
        segments=[_seg(TOOL_RESULT, "tool:a", 100)],
        bytes_per_token=1.0,
        previously_shed=frozenset(),
    )
    assert d.shed_keys == ("tool:a",) and d.estimated_tokens_after == 890


def test_protected_overflow_refuses_naming_the_ceiling() -> None:
    with pytest.raises(ContextBudgetRefusal) as e:
        decide_context_budget(
            policy=_policy(),
            request_bytes=1200,
            segments=[_seg(TOOL_RESULT, "tool:a", 100)],
            bytes_per_token=1.0,
            previously_shed=frozenset(),
        )
    assert e.value.ceiling == "tokens" and e.value.estimated_tokens == 1100
    with pytest.raises(ContextBudgetRefusal) as e:
        decide_context_budget(
            policy=_policy(cap=10**9, max_bytes=500),
            request_bytes=700,
            segments=[],
            bytes_per_token=1.0,
            previously_shed=frozenset(),
        )
    assert e.value.ceiling == "bytes"


def test_missing_ratio_uses_fallback_and_rounds_tokens_up() -> None:
    d = decide_context_budget(policy=_policy(cap=10**6), request_bytes=10, segments=[], bytes_per_token=None, previously_shed=frozenset())
    assert d.bytes_per_token == 3.0 and d.estimated_tokens_before == 4


def test_same_input_same_decision_and_policy_hash_changes_with_each_parameter() -> None:
    segs = [_seg(TOOL_RESULT, "tool:a", 100), _seg(ASSISTANT_PROSE, "history:1", 100)]

    def run() -> ContextBudgetDecision:
        return decide_context_budget(policy=_policy(), request_bytes=900, segments=segs, bytes_per_token=1.0, previously_shed=frozenset())

    first, second = run(), run()
    assert first == second
    assert first.policy_hash == _policy().audit_hash
    base = _policy()
    variants = [
        dataclasses.replace(base, max_prompt_tokens=base.max_prompt_tokens + 1),
        dataclasses.replace(base, max_request_bytes=base.max_request_bytes + 1),
        dataclasses.replace(base, shed_above=0.9),
        dataclasses.replace(base, shed_to=0.5),
        dataclasses.replace(base, fallback_bytes_per_token=4.0),
    ]
    hashes = {base.audit_hash, *(v.audit_hash for v in variants)}
    assert len(hashes) == 1 + len(variants)
    assert base.audit_hash == _policy().audit_hash


def test_policy_rejects_marks_out_of_order_and_non_positive_ceilings() -> None:
    with pytest.raises(ValueError):
        _policy(above=0.6, to=0.6)
    with pytest.raises(ValueError):
        _policy(above=0.5, to=0.6)
    with pytest.raises(ValueError):
        _policy(above=1.01, to=0.6)
    with pytest.raises(ValueError):
        _policy(above=0.9, to=0.0)
    with pytest.raises(ValueError):
        _policy(cap=0)
    with pytest.raises(ValueError):
        _policy(cap=-5)
    with pytest.raises(ValueError):
        _policy(max_bytes=0)
    with pytest.raises(ValueError):
        dataclasses.replace(_policy(), fallback_bytes_per_token=0.0)
    # shed_above == 1 is allowed.
    assert _policy(above=1.0, to=0.6).shed_above == 1.0


def test_segment_rejects_non_positive_saving() -> None:
    with pytest.raises(ValueError):
        _seg(TOOL_RESULT, "tool:a", 0)
    with pytest.raises(ValueError):
        _seg(TOOL_RESULT, "tool:a", -1)


def test_sticky_segments_are_shed_before_the_ranked_remainder() -> None:
    # A previously shed user request outranks nothing by kind, yet it is shed
    # first: the sticky set is kept whole before any new segment is chosen.
    segs = [
        _seg(USER_REQUEST, "history:0", 100),
        _seg(TOOL_RESULT, "tool:a", 100),
        _seg(ASSISTANT_PROSE, "history:1", 100),
    ]
    d = decide_context_budget(
        policy=_policy(), request_bytes=1000, segments=segs, bytes_per_token=1.0, previously_shed=frozenset({"history:0"})
    )
    # 1000 - 100 (sticky) = 900 is over the high mark, so shedding continues by kind to the low mark.
    assert d.shed_keys == ("history:0", "tool:a", "history:1")
    assert d.estimated_tokens_after == 700


def test_over_is_judged_on_the_size_with_the_sticky_set_already_shed() -> None:
    # Unshed the request is far over the high mark; with the sticky set shed it
    # sits inside the band, so nothing new is shed.
    segs = [
        _seg(ASSISTANT_PROSE, "history:1", 400),
        _seg(ASSISTANT_PROSE, "history:3", 90),
        _seg(USER_REQUEST, "history:0", 60),
    ]
    d = decide_context_budget(
        policy=_policy(), request_bytes=1200, segments=segs, bytes_per_token=1.0, previously_shed=frozenset({"history:1"})
    )
    assert d.shed_keys == ("history:1",)
    assert d.estimated_tokens_before == 1200 and d.estimated_tokens_after == 800


def test_sticky_set_alone_over_the_byte_ceiling_keeps_shedding() -> None:
    segs = [_seg(TOOL_RESULT, "tool:a", 100), _seg(TOOL_RESULT, "tool:b", 100)]
    d = decide_context_budget(
        policy=_policy(cap=10**9, max_bytes=500),
        request_bytes=650,
        segments=segs,
        bytes_per_token=1.0,
        previously_shed=frozenset({"tool:a"}),
    )
    assert d.shed_keys == ("tool:a", "tool:b")


def _grow_session(turns: int) -> list[tuple[int, int, tuple[str, ...], tuple[str, ...]]]:
    """Replay a growing session: 300 protected bytes; each turn adds a 60-byte request and a 90-byte reply.

    Returns per turn (unshed size, size after, shed keys, newly shed keys).
    The shed set carried to the next turn is the one this turn recorded.
    """
    rows: list[tuple[int, int, tuple[str, ...], tuple[str, ...]]] = []
    carried: frozenset[str] = frozenset()
    for turn in range(1, turns + 1):
        segs: list[SheddableSegment] = []
        for prior in range(turn - 1):
            segs.append(_seg(USER_REQUEST, f"history:{2 * prior}", 60))
            segs.append(_seg(ASSISTANT_PROSE, f"history:{2 * prior + 1}", 90))
        full = 300 + 150 * (turn - 1)
        d = decide_context_budget(policy=_policy(), request_bytes=full, segments=segs, bytes_per_token=1.0, previously_shed=carried)
        newly = tuple(key for key in d.shed_keys if key not in carried)
        rows.append((full, d.estimated_tokens_after, d.shed_keys, newly))
        carried = frozenset(d.shed_keys)
    return rows


def test_growing_session_sheds_once_then_holds_the_band_until_the_kept_size_crosses_the_high_mark() -> None:
    rows = _grow_session(10)
    # Turns 1-4 are under the high mark.
    assert all(shed == () for _full, _after, shed, _newly in rows[:4])
    # Turn 5 crosses it (900 > 850) and sheds the four prior replies to reach the low mark.
    assert rows[4] == (900, 540, ("history:1", "history:3", "history:5", "history:7"), ("history:1", "history:3", "history:5", "history:7"))
    # Turns 6 and 7: unshed 1050 and 1200, but with the sticky set shed 690 and 840 — inside the band.
    assert rows[5] == (1050, 690, rows[4][2], ())
    assert rows[6] == (1200, 840, rows[4][2], ())
    # The reply produced on turn 5 (history:9) is still sent on turn 6, and turn 6's (history:11) on turn 7.
    assert "history:9" not in rows[5][2] and "history:11" not in rows[6][2]
    # Turn 8: the kept size 990 crosses the high mark, so it sheds again to the low mark.
    assert rows[7][0] == 1350 and rows[7][1] == 600
    assert rows[7][3] == ("history:9", "history:11", "history:13", "history:0", "history:2")
    # Turn 9 holds; turn 10 (kept 900) sheds again.
    assert rows[8][3] == () and rows[8][1] == 750
    assert rows[9][3] != () and rows[9][1] == 600


def test_within_a_turn_tool_stubs_hold_the_band_between_sheds() -> None:
    # 300 protected bytes; every call adds a tool round of 150 bytes of which
    # stubbing saves 120. The latest round is protected, so it is no segment.
    carried: set[str] = set()
    newly_by_call: list[tuple[str, ...]] = []
    after_by_call: list[int] = []
    for call in range(1, 10):
        segs = [_seg(TOOL_RESULT, f"tool:c{earlier}", 120) for earlier in range(1, call)]
        full = 300 + 150 * call
        d = decide_context_budget(
            policy=_policy(), request_bytes=full, segments=segs, bytes_per_token=1.0, previously_shed=frozenset(carried)
        )
        newly_by_call.append(tuple(key for key in d.shed_keys if key not in carried))
        after_by_call.append(d.estimated_tokens_after)
        # Within a turn every earlier call's stubs stay stubbed.
        assert carried <= set(d.shed_keys)
        carried.update(d.shed_keys)
    # Call 4 (900) is the first over the high mark: it stubs the three earlier results.
    assert newly_by_call[:4] == [(), (), (), ("tool:c1", "tool:c2", "tool:c3")]
    assert after_by_call[3] == 540
    # Calls 5 and 6 stub nothing new (kept 690, 840); call 7 (kept 990) stubs again.
    assert newly_by_call[4:6] == [(), ()]
    assert after_by_call[4:6] == [690, 840]
    assert newly_by_call[6] == ("tool:c4", "tool:c5", "tool:c6") and after_by_call[6] == 630
    assert newly_by_call[7] == ()


def test_one_measure_serves_both_ceilings_and_a_refusal_reports_it() -> None:
    # Over the byte ceiling and far under the token marks: shed by bytes.
    d = decide_context_budget(
        policy=_policy(cap=1000, max_bytes=500),
        request_bytes=560,
        segments=[_seg(TOOL_RESULT, "tool:a", 100)],
        bytes_per_token=1.0,
        previously_shed=frozenset(),
    )
    assert d.shed_keys == ("tool:a",)
    assert (d.request_bytes_before, d.request_bytes_after) == (560, 460)
    assert (d.estimated_tokens_before, d.estimated_tokens_after) == (560, 460)
    with pytest.raises(ContextBudgetRefusal) as e:
        decide_context_budget(
            policy=_policy(cap=1000, max_bytes=500), request_bytes=560, segments=[], bytes_per_token=1.0, previously_shed=frozenset()
        )
    assert (e.value.ceiling, e.value.estimated_tokens, e.value.request_bytes) == ("bytes", 560, 560)
    with pytest.raises(ContextBudgetRefusal) as e:
        decide_context_budget(
            policy=_policy(cap=1000, max_bytes=10_000), request_bytes=1200, segments=[], bytes_per_token=1.0, previously_shed=frozenset()
        )
    assert (e.value.ceiling, e.value.estimated_tokens, e.value.request_bytes) == ("tokens", 1200, 1200)


def test_sticky_set_is_kept_while_the_unshed_request_is_over_the_byte_ceiling_even_at_the_low_mark() -> None:
    d = decide_context_budget(
        policy=_policy(cap=1000, max_bytes=500),
        request_bytes=560,
        segments=[_seg(TOOL_RESULT, "tool:a", 100), _seg(TOOL_RESULT, "tool:b", 100)],
        bytes_per_token=1.0,
        previously_shed=frozenset({"tool:a"}),
    )
    assert d.shed_keys == ("tool:a",)


@pytest.mark.parametrize("ratio", [float("inf"), float("-inf"), float("nan"), 0.0, -1.0])
def test_a_ratio_that_is_not_a_finite_positive_number_is_rejected(ratio: float) -> None:
    """An infinite ratio would estimate zero tokens for any request and switch the token ceiling off."""
    with pytest.raises(ValueError, match="bytes_per_token must be a finite positive number"):
        decide_context_budget(policy=_policy(), request_bytes=10**9, segments=[], bytes_per_token=ratio, previously_shed=frozenset())


def test_held_keys_stay_shed_when_the_unshed_request_is_back_under_the_low_mark() -> None:
    """Within a turn a stubbed tool result stays stubbed; only cross-turn history follows the release rule."""
    segs = [_seg(TOOL_RESULT, "tool:a", 100), _seg(ASSISTANT_PROSE, "history:1", 100), _seg(TOOL_RESULT, "tool:b", 100)]
    d = decide_context_budget(
        policy=_policy(),
        request_bytes=500,
        segments=segs,
        bytes_per_token=1.0,
        previously_shed=frozenset({"history:1", "tool:a"}),
        held=frozenset({"tool:a"}),
    )
    # 500 is under the low mark: the history message is restored, the held stub is not, nothing new is shed.
    assert d.shed_keys == ("tool:a",)
    assert d.estimated_tokens_after == 400
    # Control: without the hold, the same request releases everything.
    released = decide_context_budget(
        policy=_policy(), request_bytes=500, segments=segs, bytes_per_token=1.0, previously_shed=frozenset({"history:1", "tool:a"})
    )
    assert released.shed_keys == ()


def test_held_keys_count_as_already_shed_above_the_low_mark_and_keep_segment_order() -> None:
    segs = [_seg(ASSISTANT_PROSE, "history:1", 100), _seg(TOOL_RESULT, "tool:a", 100), _seg(TOOL_RESULT, "tool:b", 100)]
    d = decide_context_budget(
        policy=_policy(),
        request_bytes=1000,
        segments=segs,
        bytes_per_token=1.0,
        previously_shed=frozenset({"history:1"}),
        held=frozenset({"tool:a"}),
    )
    # 1000 - 200 = 800 is inside the band: exactly the sticky and held sets, in segment order.
    assert d.shed_keys == ("history:1", "tool:a")
    # A held key that matches no segment is ignored.
    d = decide_context_budget(
        policy=_policy(), request_bytes=500, segments=segs, bytes_per_token=1.0, previously_shed=frozenset(), held=frozenset({"tool:gone"})
    )
    assert d.shed_keys == ()
