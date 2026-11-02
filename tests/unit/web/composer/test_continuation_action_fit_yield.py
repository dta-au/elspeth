"""The automatic successor plan requires an actual unfit, resumable Compose cut."""

from __future__ import annotations

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import EffectFrontierEntry
from elspeth.web.composer.continuation_action_fit_yield import plan_action_fit_successor
from elspeth.web.composer.continuation_next_action import next_continuation_action_policy
from tests.unit.web.composer.test_continuation_cut_coverage import HANDOFF_SAFE_CUTS, POLICY, _compose, _selected, _stack


def _first_compose():
    return _selected(
        _stack(
            _compose(
                slice_sequence=1,
                composition_total=0,
                discovery_total=0,
                advisor_calls=0,
                mutation_success_seen=False,
            )
        )
    )


def test_unconsumed_first_compose_stays_with_its_same_sequence_initializer() -> None:
    selected = _first_compose()
    action = next_continuation_action_policy(selected=selected, policy=POLICY)
    assert plan_action_fit_successor(selected=selected, policy=POLICY, action=action, sequence=1, hard_remaining=0.0) is None
    # A rolled slice number cannot substitute for the original paid PRIMARY
    # source that the current cold owner requires.
    rolled = _selected(selected.stack.model_copy(update={"frames": (selected.stack.frames[0].model_copy(update={"slice_sequence": 2}),)}))
    assert plan_action_fit_successor(selected=rolled, policy=POLICY, action=action, sequence=2, hard_remaining=0.0) is None


def test_later_compose_action_fit_keeps_the_exact_slice_and_source() -> None:
    stack, _ = HANDOFF_SAFE_CUTS["later-compose"]
    selected = _selected(stack)
    action = next_continuation_action_policy(selected=selected, policy=POLICY)
    required = action.total + POLICY.segment.reserve
    plan = plan_action_fit_successor(selected=selected, policy=POLICY, action=action, sequence=2, hard_remaining=0.0)
    assert plan is not None and plan.expected_head is None
    assert plan_action_fit_successor(selected=selected, policy=POLICY, action=action, sequence=2, hard_remaining=required) is None
    with pytest.raises(AuditIntegrityError, match="slice differs"):
        plan_action_fit_successor(selected=selected, policy=POLICY, action=action, sequence=1, hard_remaining=0.0)


@pytest.mark.parametrize("cut", ("advisor-input", "end-review", "end-repair", "end-publication", "tool-ready"))
def test_other_cuts_keep_their_current_same_sequence_owner(cut: str) -> None:
    stack, _ = HANDOFF_SAFE_CUTS[cut]
    selected = _selected(stack)
    assert (
        plan_action_fit_successor(
            selected=selected,
            policy=POLICY,
            action=POLICY.actions.primary,
            sequence=2,
            hard_remaining=0.0,
        )
        is None
    )


def test_open_effect_cannot_be_turned_into_an_automatic_successor() -> None:
    selected = _first_compose()
    open_effect = EffectFrontierEntry(effect_id="provider:0:0", disposition="dispatched", receipt_id=None)
    unsafe = _selected(selected.stack.model_copy(update={"effect_frontier": (open_effect,)}))
    assert unsafe.stack.is_handoff_safe is False
    assert (
        plan_action_fit_successor(
            selected=unsafe,
            policy=POLICY,
            action=POLICY.actions.primary,
            sequence=1,
            hard_remaining=0.0,
        )
        is None
    )


def test_action_fit_rejects_a_substituted_allowance() -> None:
    stack, _ = HANDOFF_SAFE_CUTS["later-compose"]
    selected = _selected(stack)
    actual = next_continuation_action_policy(selected=selected, policy=POLICY)
    changed = actual.model_copy(update={"physical": actual.physical + 1.0})
    with pytest.raises(AuditIntegrityError, match="allowance differs"):
        plan_action_fit_successor(selected=selected, policy=POLICY, action=changed, sequence=2, hard_remaining=0.0)
