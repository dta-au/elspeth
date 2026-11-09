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
